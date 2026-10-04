import time

from django.core.management.base import BaseCommand

from jobs.scheduling import run_due_schedules
from jobs.services import fail_expired_leases, recover_unenqueued_jobs
from mailing.services.campaigns import dispatch_due_campaigns


class Command(BaseCommand):
    help = (
        "Dispatch due campaigns and Relay schedules, fail expired ack-lease tasks, and recover queued "
        "jobs that were not enqueued."
    )

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=float, default=15.0)

    def handle(self, *args, **options):
        while True:
            expired = fail_expired_leases()
            recovered = recover_unenqueued_jobs()
            fired = run_due_schedules()
            campaigns = dispatch_due_campaigns()
            if expired or recovered or fired or campaigns:
                self.stdout.write(
                    f"leases_expired={expired} recovered={recovered} schedules_fired={len(fired)} campaigns_dispatched={len(campaigns)}"
                )
            if options["once"]:
                return
            time.sleep(max(options["interval"], 1.0))
