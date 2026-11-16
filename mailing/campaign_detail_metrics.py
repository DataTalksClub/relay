"""Read-only presentation metrics for the operator campaign detail page."""
from datetime import timedelta

from django.utils import timezone

from mailing.models import CampaignRecipient, CampaignStatus


def campaign_delivery_context(campaign, progress):
    sampled_at = timezone.now()
    active = campaign.status in {CampaignStatus.QUEUED, CampaignStatus.SENDING} and not campaign.send_halted_at
    recent_count = 0
    if active:
        recent_count = CampaignRecipient.objects.filter(
            campaign=campaign,
            sent_at__gte=sampled_at - timedelta(seconds=60),
            sent_at__lte=sampled_at,
        ).count()
    # Historical imports can have campaign counters without individual send timestamps.
    metadata = campaign.metadata if isinstance(campaign.metadata, dict) else {}
    accepted = max(progress.sent_count, campaign.sent_count) if metadata.get("historical_import") else progress.sent_count
    total = campaign.recipient_count
    finished = min(total, accepted) if total else 0
    return {
        "delivery_overview": {
            "sent": accepted,
            "pending": progress.queued_count,
            "total": total,
            "finished": finished,
            "percent": min(100, round(finished * 100 / total)) if total else 0,
            "recent_per_second": f"{recent_count / 60:.2f}",
            "recent_per_minute": recent_count,
            "active": active,
            "sampled_at": sampled_at,
        }
    }
