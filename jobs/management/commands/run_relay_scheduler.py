import time

from django.core.management.base import BaseCommand

from jobs.scheduling import run_due_schedules
from jobs.services import fail_expired_leases, recover_unenqueued_jobs


class Command(BaseCommand):
    help = (
        "Run due Relay schedules, fail expired ack-lease tasks, and recover queued "
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
            if expired or recovered or fired:
                self.stdout.write(
                    f"leases_expired={expired} recovered={recovered} schedules_fired={len(fired)}"
                )
            if options["once"]:
                return
            time.sleep(max(options["interval"], 1.0))
