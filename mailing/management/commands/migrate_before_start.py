from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connections

# Stable across releases and all Relay container roles on the same database.
MIGRATION_LOCK_ID = 72634567234901


class Command(BaseCommand):
    help = "Apply migrations before starting a container, serialized across PostgreSQL containers."

    def handle(self, *args, **options):
        connection = connections["default"]
        if connection.vendor != "postgresql":
            call_command("migrate", interactive=False, verbosity=options["verbosity"])
            return

        # A session lock survives the transactions that individual migrations use.
        # Losing the connection also releases it, including on container termination.
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [MIGRATION_LOCK_ID])
        try:
            call_command("migrate", interactive=False, verbosity=options["verbosity"])
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [MIGRATION_LOCK_ID])
