"""Audited override of the duplicate-send guard.

    # Re-send a whole campaign whose content was already sent (e.g. a deliberate re-mail):
    python manage.py allow_duplicate_send --client aishl --campaign 302 --by alexey --reason "approved resend"

    # One address + content hash (the hash is in the duplicate_blocked event metadata):
    python manage.py allow_duplicate_send --client aishl --email a@b.com --content-hash <sha256> --by ... --reason ...

Overrides expire (default 24h), are never client-wide, and every use is counted and logged.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from mailing.models import Campaign, Client, SendDedupeOverride
from mailing.services.send_dedupe import normalise_email


class Command(BaseCommand):
    help = "Create an audited, expiring override for the duplicate-send guard."

    def add_arguments(self, parser):
        parser.add_argument("--client", required=True, help="client slug")
        parser.add_argument("--campaign", type=int)
        parser.add_argument("--email")
        parser.add_argument("--content-hash")
        parser.add_argument("--by", required=True, help="operator name, recorded for audit")
        parser.add_argument("--reason", required=True)
        parser.add_argument("--hours", type=int, default=24)

    def handle(self, *args, **options):
        try:
            client = Client.objects.get(slug=options["client"])
        except Client.DoesNotExist as exc:
            raise CommandError(f"unknown client {options['client']}") from exc
        campaign = None
        if options["campaign"]:
            campaign = Campaign.objects.filter(pk=options["campaign"], client=client).first()
            if campaign is None:
                raise CommandError("campaign not found for this client")
        elif not (options["email"] and options["content_hash"]):
            raise CommandError("give --campaign, or both --email and --content-hash (overrides are never client-wide)")
        if not options["reason"].strip():
            raise CommandError("--reason must not be empty")
        override = SendDedupeOverride.objects.create(
            client=client, campaign=campaign, email=normalise_email(options["email"]),
            content_hash=options["content_hash"] or "", reason=options["reason"], created_by=options["by"],
            expires_at=timezone.now() + timedelta(hours=options["hours"]),
        )
        self.stdout.write(f"override {override.pk} created, expires {override.expires_at.isoformat()}")
