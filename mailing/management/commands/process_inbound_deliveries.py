import time

from django.core.management.base import BaseCommand

from mailing.services.inbound_routing import process_due_deliveries


class Command(BaseCommand):
    help = 'Dispatch inbound mailbox deliveries with durable retries.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        while True:
            process_due_deliveries()
            if options['once']:
                return
            time.sleep(2)
