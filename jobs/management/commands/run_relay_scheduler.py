import logging

from django.core.management.base import BaseCommand
from django.db import DatabaseError, InterfaceError

from jobs.scheduling import run_due_schedules
from jobs.services import (
    fail_expired_leases,
    prune_finished_task_results,
    record_scheduler_heartbeat,
    recover_unenqueued_jobs,
)
from mailing import db_retry
from mailing.db_retry import is_transient_db_error
from mailing.services.campaigns import dispatch_due_campaigns
from mailing.services.ses_monitor import check_ses_delivery_gap
from mailing.services.transactional_monitor import check_transactional_health
from mailing.services.transactional_reliability import alert_stuck_transactional_messages

logger = logging.getLogger(__name__)

# One pass retries a short outage (a Coolify network reattach or a Postgres
# IP churn costs seconds, not minutes) instead of killing the loop: an
# exited scheduler leaves a stale heartbeat while the container looks
# healthy, which is exactly the invisible stall the watchdog watches for.
PASS_RETRY_ATTEMPTS = 3
PASS_RETRY_BASE_SECONDS = 1.0
PASS_RETRY_MAX_SECONDS = 8.0


class Command(BaseCommand):
    help = (
        "Dispatch due campaigns and Relay schedules, fail expired ack-lease tasks, recover queued "
        "jobs that were not enqueued, and prune finished task results past retention. Each pass "
        "records a scheduler heartbeat."
    )

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=float, default=15.0)

    def _run_single_pass(self):
        expired = fail_expired_leases()
        recovered = recover_unenqueued_jobs()
        fired = run_due_schedules()
        campaigns = dispatch_due_campaigns()
        pruned = prune_finished_task_results()
        alert_stuck_transactional_messages()
        check_transactional_health()
        check_ses_delivery_gap()
        # The one liveness signal this loop has. Written on every pass,
        # including the empty ones, because "nothing to do" and "not
        # running" have to look different to anything reading the row.
        record_scheduler_heartbeat(
            schedules_fired=len(fired),
            expired_leases=expired,
            recovered_jobs=recovered,
            campaigns_dispatched=len(campaigns),
        )
        if expired or recovered or fired or campaigns or pruned:
            self.stdout.write(
                f"leases_expired={expired} recovered={recovered} schedules_fired={len(fired)} campaigns_dispatched={len(campaigns)} task_results_pruned={bool(pruned)}"
            )

    def handle(self, *args, **options):
        while True:
            try:
                db_retry.run_with_db_retry(
                    self._run_single_pass,
                    action="relay scheduler pass",
                    attempts=PASS_RETRY_ATTEMPTS,
                    base_delay_seconds=PASS_RETRY_BASE_SECONDS,
                    max_delay_seconds=PASS_RETRY_MAX_SECONDS,
                )
            except (DatabaseError, InterfaceError) as exc:
                if not is_transient_db_error(exc):
                    raise
                # Still failing after the pass-level retries: a longer outage
                # than one pass can absorb. In continuous mode wait out the
                # outage instead of exiting; with --once there is no next
                # pass, so fail loudly. Both sleeps go through db_retry.sleep
                # so tests can freeze them.
                if options["once"]:
                    raise
                logger.warning("relay scheduler pass still failing; waiting out the outage: %s", exc)
                db_retry.sleep(PASS_RETRY_MAX_SECONDS)
                continue
            if options["once"]:
                return
            db_retry.sleep(max(options["interval"], 1.0))
