"""``db_worker`` with database self-healing for long-lived containers.

Overrides ``django_tasks_db``'s command of the same name (the ``relay`` app is
listed before ``django_tasks_db`` in ``INSTALLED_APPS``), so the Coolify
service commands stay ``python manage.py db_worker ...`` unchanged.

Upstream's poll loop lets any database error in the poll (claiming the next
task) escape and kill the process, and only drops a broken connection after
a task ran. After the 2026-10-22/23 Postgres IP change the workers kept dead
sockets to the old IP until a manual restart. This worker:

* drops unusable/expired connections before every poll, so with
  ``CONN_HEALTH_CHECKS`` a dead socket is replaced by a fresh connection
  that re-resolves the database host name;
* retries a transient DB error in the poll with backoff and waits out a
  longer outage instead of exiting (``--batch`` still fails loudly);
* touches the DB heartbeat file after each successful poll, which
  ``worker_healthcheck`` checks.

The loop body mirrors ``django_tasks_db`` 0.12 ``Worker.run`` (see
``tests/test_db_worker_resilience.py`` which pins its source) and keeps the
"Starting worker worker_id=... queues=..." banner the host watchdog keys on.
"""

import logging
import random
import time

from django.db import DatabaseError, InterfaceError, connections
from django.db.utils import OperationalError
from django_tasks_db.management.commands import db_worker as upstream
from django_tasks_db.models import DBTaskResult
from django_tasks_db.utils import exclusive_transaction

from mailing import db_retry
from mailing.db_retry import first_line, is_transient_db_error, run_loop_iteration

logger = logging.getLogger("django_tasks_db")


class ResilientWorker(upstream.Worker):
    def _claim_next_task(self):
        tasks = DBTaskResult.objects.ready().filter(backend_name=self.backend_name)
        if not self.process_all_queues:
            tasks = tasks.filter(queue_name__in=self.queue_names)

        # During this transaction, all "ready" tasks are locked. Therefore, it's
        # important it be as efficient as possible.
        with exclusive_transaction(tasks.db):
            try:
                task_result = tasks.get_locked()
            except OperationalError as e:
                # Ignore locked databases (SQLite) and keep trying.
                if "is locked" in e.args[0]:
                    task_result = None
                else:
                    raise

            if task_result is not None:
                # "claim" the task, so it isn't run by another worker process
                task_result.claim(self.worker_id)
        return task_result

    def run(self) -> None:
        logger.info(
            "Starting worker worker_id=%s queues=%s",
            self.worker_id,
            ",".join(self.queue_names),
        )

        if self.startup_delay and self.interval:
            # Add a random small delay before starting to avoid a thundering herd
            time.sleep(random.random())  # noqa: S311

        while self.running:
            completed, task_result = run_loop_iteration(
                self._claim_next_task, action="db_worker poll", once=self.batch
            )
            if not completed:
                continue

            if task_result is not None:
                try:
                    self.run_task(task_result)
                except (DatabaseError, InterfaceError) as exc:
                    # run_task records task errors itself; one escaping here
                    # means recording the outcome failed (the database went
                    # away mid-task). Upstream would exit; keep polling and
                    # let the next iteration reconnect. The row stays RUNNING,
                    # exactly as it would after a crash.
                    if self.batch or not is_transient_db_error(exc):
                        raise
                    logger.warning(
                        "db_worker: retrying transient DB error after task %s could not be recorded: %s",
                        task_result.id,
                        first_line(exc),
                    )
                    connections.close_all()
                    db_retry.sleep(db_retry.LOOP_OUTAGE_SLEEP_SECONDS)
                    # No ``continue``: upstream's run_task already counted the
                    # task, so the --max-tasks check below must still run.

            if self.batch and task_result is None:
                logger.info(
                    "No more tasks to run for worker_id=%s - exiting gracefully.",
                    self.worker_id,
                )
                return None

            if self.max_tasks is not None and self._run_tasks >= self.max_tasks:
                logger.info(
                    "Run maximum tasks (%d) on worker=%s - exiting gracefully.",
                    self._run_tasks,
                    self.worker_id,
                )
                return None

            # If ctrl-c has just interrupted a task, self.running was cleared,
            # and we should not sleep, but rather exit immediately.
            if self.running and not task_result:
                time.sleep(self.interval)


class Command(upstream.Command):
    def handle(
        self,
        *,
        verbosity,
        queue_name,
        interval,
        batch,
        backend_name,
        startup_delay,
        reload,
        max_tasks,
        worker_id,
        **options,
    ):
        self.configure_logging(verbosity)

        if reload and batch:
            logger.warning("Warning: --reload and --batch cannot be specified together. Disabling autoreload.")
            reload = False

        worker = ResilientWorker(
            queue_names=queue_name.split(","),
            interval=interval,
            batch=batch,
            backend_name=backend_name,
            startup_delay=startup_delay,
            max_tasks=max_tasks,
            worker_id=worker_id,
        )

        if reload:
            if upstream.os.environ.get(upstream.DJANGO_AUTORELOAD_ENV) == "true":
                # Only the child process should configure its signals
                worker.configure_signals()

            upstream.run_with_reloader(worker.run)
        else:
            worker.configure_signals()
            worker.run()
