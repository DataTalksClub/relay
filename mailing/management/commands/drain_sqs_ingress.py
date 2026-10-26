"""Poll an AWS-fed SQS queue and hand each message to the task system.

Runs alongside the task worker. This is the only SQS consumer left: the queues
Django used to write to moved onto django.tasks, and the two that remain are
fed by AWS itself.
"""

import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from mailing.ingress import INGRESS_WORKER_NAMES, get_ingress_config
from mailing.sqs_worker import SqsWorker


class Command(BaseCommand):
    help = "Drain an AWS-fed SQS queue by enqueuing the matching background task."

    def add_arguments(self, parser):
        parser.add_argument("worker", choices=INGRESS_WORKER_NAMES)
        parser.add_argument(
            "--once", action="store_true", help="Process at most one batch and exit."
        )
        parser.add_argument("--batch-size", type=int, default=10)
        parser.add_argument("--wait-time", type=int, default=20)
        parser.add_argument("--idle-sleep", type=float, default=0)
        parser.add_argument(
            "--heartbeat-every",
            type=int,
            default=30,
            help="Log a heartbeat line every N consecutive idle polls.",
        )

    def handle(self, *args, **options):
        stop_requested = False

        def request_stop(signum, frame):
            nonlocal stop_requested
            stop_requested = True

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

        name = options["worker"]
        if name == "aisl-ses-webhooks" and not settings.AISL_SES_FORWARD_ENABLED:
            self.stdout.write("AISHL SES ingress standby (AISL_SES_FORWARD_ENABLED=0); no messages consumed")
            while not options["once"] and not stop_requested:
                time.sleep(1)
            return
        worker = SqsWorker(
            get_ingress_config(name),
            batch_size=options["batch_size"],
            wait_time=options["wait_time"],
        )

        self.stdout.write(f"Starting {name} ingress drain")
        if options["once"]:
            result = worker.run_once()
            self.stdout.write(
                f"received={result.received} deleted={result.deleted} failed={result.failed}"
            )
            return

        heartbeat_every = options["heartbeat_every"]
        if heartbeat_every < 1:
            raise CommandError("--heartbeat-every must be >= 1")
        idle_polls = 0
        for result in worker.run_forever(
            should_stop=lambda: stop_requested, idle_sleep=options["idle_sleep"]
        ):
            if result.received:
                idle_polls = 0
                self.stdout.write(
                    f"received={result.received} deleted={result.deleted} failed={result.failed}"
                )
            else:
                idle_polls += 1
                if idle_polls % heartbeat_every == 0:
                    self.stdout.write(
                        f"heartbeat {name} idle_polls={idle_polls} "
                        f"received={result.received} deleted={result.deleted} failed={result.failed}"
                    )

        self.stdout.write(f"Stopped {name} ingress drain")
