"""Create receiving addresses from the environment routes.

The cutover path. A domain that was already receiving mail through
INBOUND_EMAIL_ROUTES has addresses the application does not know about, and
without this command the first message after the receipt rule is repointed
finds no managed address and is discarded as unmatched.

Safe to run repeatedly. An address that already exists is left alone, so
retiring an address is not undone by running this again -- which matters,
because re-running it must not resurrect a deliberately retired address.

    python manage.py provision_inbound_addresses
    python manage.py provision_inbound_addresses --dry-run
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from mailing.services.inbound_views import backfill_from_routes


class Command(BaseCommand):
    help = "Create a receiving address for every route in INBOUND_EMAIL_ROUTES."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be created without writing anything.",
        )

    def handle(self, *args, **options):
        routes = settings.INBOUND_EMAIL_ROUTES
        if not routes:
            self.stdout.write(
                self.style.WARNING(
                    "INBOUND_EMAIL_ROUTES is empty, so there is nothing to create. "
                    "Receiving addresses are managed in the console under "
                    "Configure > Receiving addresses."
                )
            )
            return

        if options["dry_run"]:
            self.stdout.write(f"{len(routes)} route(s) configured; would create the missing ones:")
            for address in sorted(routes):
                self.stdout.write(f"  {address}")
            return

        created = backfill_from_routes(routes)
        if not created:
            self.stdout.write(f"All {len(routes)} route(s) already have a receiving address.")
            return
        for row in created:
            self.stdout.write(self.style.SUCCESS(f"Created {row.address}"))
        self.stdout.write(f"{len(created)} address(es) created from INBOUND_EMAIL_ROUTES.")
