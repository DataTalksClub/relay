"""Per-link click statistics from the campaign's tracked click events."""

from django.db.models import Case, CharField, Count, Q, Value, When
from django.db.models.fields.json import KeyTextTransform, KeyTransform
from django.db.models.functions import Cast, Coalesce, Concat

from mailing.models import Campaign, EmailEvent, EmailEventType


def clicker_identity():
    source = KeyTransform("mailchimp", "metadata")
    source_id = Case(
        When(
            Q(metadata__provider="mailchimp", metadata__historical_import=True)
            & Q(metadata__mailchimp__datacenter__regex=r"^[a-z]{2}[0-9]+$")
            & Q(metadata__mailchimp__list_id__regex=r"^[A-Za-z0-9_-]{1,255}$")
            & Q(metadata__mailchimp__email_id__regex=r"^[A-Za-z0-9_-]{1,255}$")
            & ~Q(metadata__mailchimp__list_id=None)
            & ~Q(metadata__mailchimp__email_id=None),
            then=Concat(
                Value("mailchimp:"), KeyTextTransform("datacenter", source), Value(":"),
                KeyTextTransform("list_id", source), Value(":"), KeyTextTransform("email_id", source),
            ),
        ),
        default=Value(None),
        output_field=CharField(),
    )
    return Coalesce(
        Cast("contact_id", CharField()), Cast("campaign_recipient__contact_id", CharField()), source_id,
    )


def campaign_link_click_stats(campaign: Campaign) -> list[dict]:
    """Count known contacts or exact unlinked source identities once per link."""
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
            unique_clickers=Count(clicker_identity(), distinct=True),
        )
        .order_by("-total_clicks", "url")
    )


def campaign_link_click_payload(campaign: Campaign) -> dict:
    return {
        "campaign": campaign.external_key,
        "links": campaign_link_click_stats(campaign),
    }
