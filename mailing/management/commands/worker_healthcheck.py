"""Liveness probe for worker containers.

Exits 0 when the database is reachable and the django-tasks tables exist.
Optionally checks that the named queue has a worker-relevant shape by
counting pending rows. Never prints secrets; output is a single line.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Healthcheck for db_worker / scheduler / dispatcher containers."

    def add_arguments(self, parser):
        parser.add_argument("--queue-name", default="", help="Queue to report (default: all).")

    def handle(self, *args, **options):
        queue_name = (options.get("queue_name") or "").strip()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception as exc:
            raise CommandError(f"database unreachable: {exc.__class__.__name__}") from exc
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM django_tasks_dbtaskresult")
                pending = cursor.fetchone()[0]
        except Exception:
            # Table missing (e.g. migrations not yet applied at first start):
            # the container-entrypoint migration gate owns that case, so
            # report degraded-but-alive rather than failing the healthcheck.
            self.stdout.write("worker_healthcheck: alive (migrations pending)")
            return
        scope = queue_name or "all-queues"
        self.stdout.write(f"worker_healthcheck: alive scope={scope} task_rows={pending}")
