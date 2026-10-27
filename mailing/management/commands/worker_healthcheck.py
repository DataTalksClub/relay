"""Liveness probe for worker containers (Docker ``healthcheck``).

Exits non-zero when either:

* a real ``SELECT 1`` round trip to the database fails or does not finish
  within ``--timeout`` seconds (connect and statement both bounded), or
* the worker process in this container has stopped completing DB-backed loop
  iterations: the heartbeat file it touches after every successful iteration
  (``RELAY_DB_HEARTBEAT_FILE``, see ``mailing/db_retry.py``) is older than
  ``--max-heartbeat-age`` seconds.

The second check is what catches a genuinely stuck worker. This probe runs as
a fresh process with fresh DNS, so on 2026-10-22/23 it would have passed while
the long-lived worker was still talking to Postgres' old IP. A missing
heartbeat file (process not yet through its first iteration, or a container
whose command does not write one) only skips that check.

Optionally reports the named queue's task-row count. Never prints secrets;
output is a single line.
"""

import os
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connection, transaction
from django_tasks_db.models import DBTaskResult

DEFAULT_TIMEOUT_SECONDS = 5
# Generous on purpose: a db_worker running one long task (a large campaign
# batch, a big recipient-list import) does not poll until it finishes.
DEFAULT_MAX_HEARTBEAT_AGE_SECONDS = 900


class Command(BaseCommand):
    help = "Healthcheck for db_worker / scheduler / dispatcher containers."

    def add_arguments(self, parser):
        parser.add_argument("--queue-name", default="", help="Queue to report (default: all).")
        parser.add_argument(
            "--timeout",
            type=int,
            default=DEFAULT_TIMEOUT_SECONDS,
            help="Seconds allowed for the database connect and for SELECT 1.",
        )
        parser.add_argument(
            "--max-heartbeat-age",
            type=int,
            default=DEFAULT_MAX_HEARTBEAT_AGE_SECONDS,
            help="Fail when this container's worker DB heartbeat is older than this (0 disables).",
        )

    def handle(self, *args, **options):
        queue_name = (options.get("queue_name") or "").strip()
        timeout = max(1, options["timeout"])
        self._check_round_trip(timeout)
        heartbeat_age = self._check_heartbeat(options["max_heartbeat_age"])
        heartbeat = "none" if heartbeat_age is None else f"{heartbeat_age}s"
        try:
            # The real table name: the old hard-coded "django_tasks_dbtaskresult"
            # never existed, so every probe reported "migrations pending".
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) FROM {connection.ops.quote_name(DBTaskResult._meta.db_table)}")
                pending = cursor.fetchone()[0]
        except DatabaseError:
            # Table missing (e.g. migrations not yet applied at first start):
            # the container-entrypoint migration gate owns that case, so
            # report degraded-but-alive rather than failing the healthcheck.
            self.stdout.write(f"worker_healthcheck: alive (migrations pending) heartbeat_age={heartbeat}")
            return
        scope = queue_name or "all-queues"
        self.stdout.write(f"worker_healthcheck: alive scope={scope} task_rows={pending} heartbeat_age={heartbeat}")

    def _check_round_trip(self, timeout):
        if connection.vendor == "postgresql":
            options = connection.settings_dict.setdefault("OPTIONS", {})
            options["connect_timeout"] = min(int(options.get("connect_timeout") or timeout), timeout)
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                if connection.vendor == "postgresql":
                    cursor.execute("SET LOCAL statement_timeout = %s", [f"{timeout}s"])
                cursor.execute("SELECT 1")
                row = cursor.fetchone()
        except Exception as exc:
            raise CommandError(f"database unreachable: {exc.__class__.__name__}") from exc
        if not row or row[0] != 1:
            raise CommandError("database round trip returned an unexpected result")

    def _check_heartbeat(self, max_age):
        path = getattr(settings, "RELAY_DB_HEARTBEAT_FILE", "")
        if not path:
            return None
        try:
            age = int(time.time() - os.stat(path).st_mtime)
        except FileNotFoundError:
            return None
        if max_age > 0 and age > max_age:
            raise CommandError(f"worker DB heartbeat stale: {age}s > {max_age}s ({path})")
        return age
