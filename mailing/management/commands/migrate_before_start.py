import time

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, OperationalError, connections
from django.db.migrations.executor import MigrationExecutor

# Stable across releases and all Relay container roles on the same database.
MIGRATION_LOCK_ID = 72634567234901
DATABASE_CONNECT_ATTEMPTS = 30
DATABASE_CONNECT_RETRY_SECONDS = 2
MIGRATION_LOCK_TIMEOUT_SECONDS = 5
MIGRATION_LOCK_RETRY_ATTEMPTS = 3
MIGRATION_LOCK_RETRY_SECONDS = 2
TRANSIENT_CONNECTION_ERRORS = (
    "could not translate host name",
    "temporary failure in name resolution",
    "connection refused",
    "connection timed out",
    "timeout expired",
    "database system is starting up",
)


class MigrationLockTimeoutError(CommandError):
    """A bounded migration retry window expired while waiting for a DB lock."""


def _has_pending_migrations(connection):
    executor = MigrationExecutor(connection)
    return bool(executor.migration_plan(executor.loader.graph.leaf_nodes()))


def _is_lock_timeout_error(exc):
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        diag = getattr(current, "diag", None)
        sqlstate = getattr(diag, "sqlstate", None) or getattr(current, "pgcode", None)
        if sqlstate == "55P03":
            return True
        if "lock timeout" in str(current).lower():
            return True
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return False


def _apply_migrations_with_retry(connection, stdout, *, verbosity, output_stream=None):
    command_options = {"interactive": False, "verbosity": verbosity}
    if output_stream is not None:
        command_options["stdout"] = output_stream

    for attempt in range(1, MIGRATION_LOCK_RETRY_ATTEMPTS + 1):
        with connection.cursor() as cursor:
            cursor.execute(f"SET lock_timeout = '{MIGRATION_LOCK_TIMEOUT_SECONDS}s'")
        try:
            call_command("migrate", **command_options)
            return
        except DatabaseError as exc:
            if not _is_lock_timeout_error(exc):
                raise
            if attempt == MIGRATION_LOCK_RETRY_ATTEMPTS:
                raise MigrationLockTimeoutError(
                    f"Migration remained blocked by database locks after {MIGRATION_LOCK_RETRY_ATTEMPTS} attempts."
                ) from None
            stdout.write(
                f"Migration hit a database lock timeout; retrying ({attempt}/{MIGRATION_LOCK_RETRY_ATTEMPTS})."
            )
            time.sleep(MIGRATION_LOCK_RETRY_SECONDS)


def _acquire_migration_lock_with_retry(connection, stdout):
    for attempt in range(1, MIGRATION_LOCK_RETRY_ATTEMPTS + 1):
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"SET lock_timeout = '{MIGRATION_LOCK_TIMEOUT_SECONDS}s'")
                cursor.execute("SELECT pg_advisory_lock(%s)", [MIGRATION_LOCK_ID])
            return
        except DatabaseError as exc:
            if not _is_lock_timeout_error(exc):
                raise
            if attempt == MIGRATION_LOCK_RETRY_ATTEMPTS:
                raise MigrationLockTimeoutError(
                    f"Migration lock remained busy after {MIGRATION_LOCK_RETRY_ATTEMPTS} attempts."
                ) from None
            stdout.write(
                f"Migration lock is busy; retrying ({attempt}/{MIGRATION_LOCK_RETRY_ATTEMPTS})."
            )
            time.sleep(MIGRATION_LOCK_RETRY_SECONDS)


def wait_for_database(connection, stdout):
    # Coolify attaches the shared DB network shortly after starting each
    # service container. DNS can be unavailable on its very first connection.
    # Retry only connection establishment, never failed migration operations.
    for attempt in range(1, DATABASE_CONNECT_ATTEMPTS + 1):
        try:
            connection.ensure_connection()
            return
        except OperationalError as exc:
            if not any(message in str(exc).lower() for message in TRANSIENT_CONNECTION_ERRORS):
                raise
            connection.close()
            if attempt == DATABASE_CONNECT_ATTEMPTS:
                raise CommandError(
                    "Migration database connection is still unavailable after 30 attempts; startup stopped."
                ) from None
            stdout.write(f"Database connection not ready; retrying startup ({attempt}/{DATABASE_CONNECT_ATTEMPTS}).")
            time.sleep(DATABASE_CONNECT_RETRY_SECONDS)


class Command(BaseCommand):
    help = "Apply migrations before starting a container, serialized across PostgreSQL containers."

    def handle(self, *args, **options):
        connection = connections["default"]
        if connection.vendor != "postgresql":
            call_command("migrate", interactive=False, verbosity=options["verbosity"])
            return

        wait_for_database(connection, self.stdout)
        if not _has_pending_migrations(connection):
            self.stdout.write("No unapplied migrations; startup migration gate skipped.")
            return
        # A session lock survives the transactions that individual migrations use.
        # Losing the connection also releases it, including on container termination.
        _acquire_migration_lock_with_retry(connection, self.stdout)
        try:
            # Recheck inside the lock: another container may have applied the
            # same release's migrations after the first plan check.
            if not _has_pending_migrations(connection):
                self.stdout.write("Migrations were applied by another container; startup gate skipped.")
                return
            _apply_migrations_with_retry(connection, self.stdout, verbosity=options["verbosity"])
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [MIGRATION_LOCK_ID])
