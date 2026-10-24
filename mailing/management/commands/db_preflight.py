from collections import Counter

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from mailing.management.commands.migrate_before_start import MIGRATION_LOCK_ID

OBJECTS_SQL = """
SELECT c.relkind::text, format('%I.%I', n.nspname, c.relname),
       pg_get_userbyid(c.relowner)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
UNION ALL
SELECT p.prokind::text, p.oid::regprocedure::text, pg_get_userbyid(p.proowner)
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
ORDER BY 1, 2
"""


class Command(BaseCommand):
    help = "Check public object ownership and plan/apply migrations before replacing serving containers."

    def add_arguments(self, parser):
        parser.add_argument("--expected-role", required=True)
        parser.add_argument("--expected-database", required=True)

    def check_ownership(self, connection, role, database):
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user, session_user, current_database()")
            current, session, actual_database = cursor.fetchone()
            if current != role or session != role or actual_database != database:
                raise CommandError(
                    f"DB preflight identity mismatch: expected database={database} role={role}; "
                    f"got database={actual_database} current_user={current} session_user={session}."
                )
            cursor.execute(OBJECTS_SQL)
            objects = cursor.fetchall()
        counts = Counter((kind, owner) for kind, _name, owner in objects)
        self.stdout.write(f"DB preflight database={database} migration_role={role}")
        for (kind, owner), count in sorted(counts.items()):
            self.stdout.write(f"owner={owner} kind={kind} count={count}")
        wrong = [(kind, name, owner) for kind, name, owner in objects if owner != role]
        if wrong:
            for kind, name, owner in wrong:
                self.stderr.write(f"Ownership mismatch: {name} kind={kind} owner={owner}; expected={role}")
            raise CommandError(
                f"DB preflight FAILED: {len(wrong)} public objects are not owned by migration role {role}. "
                "Restore/bootstrap as the migration role or align ownership using docs/database-ownership.md. "
                "Do not replace serving containers."
            )
        self.stdout.write(f"Ownership OK: {len(objects)} public objects owned by {role}.")

    def handle(self, *args, **options):
        connection = connections["default"]
        if connection.vendor != "postgresql":
            raise CommandError("Deploy DB preflight requires PostgreSQL; refusing to skip ownership checks.")
        # Share the startup gate's session lock, including the plan and ownership
        # checks, so overlapping workflows/container starts cannot race the DDL.
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [MIGRATION_LOCK_ID])
        try:
            self.check_ownership(connection, options["expected_role"], options["expected_database"])
            self.stdout.write("Migration plan before rolling switch:")
            try:
                call_command("migrate", plan=True, interactive=False, stdout=self.stdout, verbosity=1)
                call_command("migrate", interactive=False, stdout=self.stdout, verbosity=1)
                call_command("migrate", check_unapplied=True, interactive=False, stdout=self.stdout, verbosity=0)
            except Exception as exc:
                # Keep arbitrary data-migration exceptions/connection strings out
                # of shared deploy logs. Ownership errors above name each object.
                raise CommandError(
                    f"DB preflight migration failed ({type(exc).__name__}); do not replace serving containers."
                ) from None
            self.check_ownership(connection, options["expected_role"], options["expected_database"])
            self.stdout.write(self.style.SUCCESS("DB preflight PASS; safe to start container replacement."))
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [MIGRATION_LOCK_ID])
