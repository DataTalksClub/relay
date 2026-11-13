"""One-command emergency stop for running or queued campaigns.

Performs a plain, lock-free UPDATE on ``Campaign`` so every campaign worker
halts before its next SES call (within one in-flight call) without waiting on
any row locks held by active senders.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from mailing.models import Campaign, CampaignStatus
from mailing.services.api import HALTABLE_STATUSES, PAUSE_REASON, STOP_REASON


class Command(BaseCommand):
    help = "Immediately halt a running/queued campaign (or all active campaigns) without row locks."

    def add_arguments(self, parser):
        parser.add_argument(
            "target",
            nargs="?",
            help="Campaign numeric ID or external_key to halt.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="halt_all",
            help="Halt every QUEUED or SENDING campaign.",
        )
        parser.add_argument(
            "--pause",
            action="store_true",
            help="Pause (resumable) instead of permanently stopping/cancelling.",
        )

    def handle(self, *args, **options):
        target = options["target"]
        halt_all = options["halt_all"]
        pause = options["pause"]

        if bool(target) == bool(halt_all):
            raise CommandError("Specify either <campaign_id_or_external_key> or --all.")

        now = timezone.now()
        reason = PAUSE_REASON if pause else STOP_REASON
        update_fields = {
            "send_halted_at": now,
            "send_halt_reason": reason,
            "updated_at": now,
        }
        if not pause:
            update_fields["status"] = CampaignStatus.CANCELLED

        if halt_all:
            qs = Campaign.objects.filter(status__in=HALTABLE_STATUSES, send_halted_at__isnull=True)
            if not pause:
                # Also allow stopping already-paused campaigns permanently.
                qs = Campaign.objects.filter(status__in=HALTABLE_STATUSES)
            targets = list(qs.values_list("id", "external_key"))
            if not targets:
                self.stdout.write("No active (QUEUED/SENDING) campaigns to halt.")
                return
            updated = Campaign.objects.filter(pk__in=[pk for pk, _ in targets]).update(**update_fields)
            keys = ", ".join(f"{pk}:{key or '-'}" for pk, key in targets)
            self.stdout.write(f"Halted {updated} campaign(s) ({reason}): {keys}")
            return

        lookup = Q(external_key=target)
        if target.isdigit():
            lookup = lookup | Q(pk=int(target))
        matches = list(Campaign.objects.filter(lookup).values_list("id", "external_key", "status"))
        if not matches:
            raise CommandError(f"Campaign not found: {target!r}")
        if len(matches) > 1:
            raise CommandError(f"Ambiguous campaign target {target!r}: matched IDs {[m[0] for m in matches]}")

        pk, external_key, status = matches[0]
        if status not in HALTABLE_STATUSES:
            self.stdout.write(f"Campaign {pk} ({external_key or '-'}) is in status {status!r}; nothing to halt.")
            return

        qs = Campaign.objects.filter(pk=pk, status__in=HALTABLE_STATUSES)
        if pause:
            qs = qs.filter(send_halted_at__isnull=True)
        updated = qs.update(**update_fields)
        if updated:
            self.stdout.write(f"Halted campaign {pk} ({external_key or '-'}) ({reason}).")
        else:
            self.stdout.write(f"Campaign {pk} ({external_key or '-'}) was already halted.")
