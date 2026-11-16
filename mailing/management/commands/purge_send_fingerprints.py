"""Delete duplicate-guard fingerprints older than RELAY_DEDUPE_RETENTION_DAYS (rows past every window are inert)."""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from mailing.models import SendFingerprint, SendFingerprintState


class Command(BaseCommand):
    help = "Purge old send fingerprints."

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=settings.DEDUPE_RETENTION_DAYS)
        total = 0
        while True:
            ids = list(
                SendFingerprint.objects.filter(reserved_at__lt=cutoff)
                .exclude(state=SendFingerprintState.RESERVED).values_list("pk", flat=True)[:5000]
            )
            if not ids:
                break
            total += SendFingerprint.objects.filter(pk__in=ids).delete()[0]
        self.stdout.write(f"purged {total} fingerprints older than {cutoff.isoformat()}")
