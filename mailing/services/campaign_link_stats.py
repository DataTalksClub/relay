"""Per-link click statistics from the campaign's tracked click events."""

from django.db.models import Count
from django.db.models.functions import Coalesce

from mailing.models import Campaign, EmailEvent, EmailEventType


def campaign_link_click_stats(campaign: Campaign) -> list[dict]:
    """Return click totals and distinct contacts, including imported history."""
    return list(
        EmailEvent.objects.filter(
            campaign=campaign,
            event_type=EmailEventType.CLICK,
        )
        .exclude(url="")
        .order_by()
        .values("url")
        .annotate(
            total_clicks=Count("id"),
            unique_clickers=Count(Coalesce("contact_id", "campaign_recipient__contact_id"), distinct=True),
        )
        .order_by("-total_clicks", "url")
    )


def campaign_link_click_payload(campaign: Campaign) -> dict:
    return {
        "campaign": campaign.external_key,
        "links": campaign_link_click_stats(campaign),
    }
