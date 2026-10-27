import time

from django.core.management.base import BaseCommand

from mailing.db_retry import run_loop_iteration
from mailing.services.inbound_routing import process_due_deliveries


class Command(BaseCommand):
    help = 'Dispatch inbound mailbox deliveries with durable retries.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        while True:
            completed, _ = run_loop_iteration(
                process_due_deliveries, action="inbound delivery dispatcher", once=options['once']
            )
            if not completed:
                continue
            if options['once']:
                return
            time.sleep(2)
