import time

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, connections

# Stable across releases and all Relay container roles on the same database.
MIGRATION_LOCK_ID = 72634567234901
DATABASE_CONNECT_ATTEMPTS = 30
DATABASE_CONNECT_RETRY_SECONDS = 2
TRANSIENT_CONNECTION_ERRORS = (
    "could not translate host name",
    "temporary failure in name resolution",
    "connection refused",
    "connection timed out",
    "timeout expired",
    "database system is starting up",
)


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
        # A session lock survives the transactions that individual migrations use.
        # Losing the connection also releases it, including on container termination.
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [MIGRATION_LOCK_ID])
        try:
            call_command("migrate", interactive=False, verbosity=options["verbosity"])
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [MIGRATION_LOCK_ID])
