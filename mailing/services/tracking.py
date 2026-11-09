import logging
from datetime import datetime
from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError, OperationalError, connection, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from mailing.models import Campaign, CampaignRecipient, CampaignRecipientStatus, EmailEvent, EmailEventType
from mailing.services.client_callbacks import emit_client_callback
from mailing.services.cmp_callbacks import emit_cmp_contact_event
from mailing.services.contacts import subscribe_contact, unsubscribe_contact
from mailing.services.tokens import get_recipient_by_unsubscribe_token, token_hash

logger = logging.getLogger(__name__)

TRANSPARENT_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
    b"\x00\x02\x02D\x01\x00;"
)

TRACKING_QUEUE = "tracking"
ENGAGEMENT_COUNTS_REFRESH_INTERVAL_SECONDS = 5

# Web side of the recipient-facing paths (/t/o, /t/c, /unsubscribe).
#
# On 2026-11-09 every open and click took Campaign FOR UPDATE and recounted the
# whole campaign inline. Sender threads hold Campaign FOR SHARE across each SES
# call, so during a send these requests queued on one row lock until every
# gunicorn thread was blocked and the proxy answered 503 for all paths. The web
# now does one indexed token lookup plus one task insert; the "tracking" worker
# applies the event with the recipient row lock only and refreshes counters
# debounced after commit. No path here takes the campaign row lock.


def is_allowed_click_destination(destination_url):
    if not destination_url:
        return False
    parsed = urlparse(destination_url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _recipient_id_for_tracking_token(raw_token):
    if not raw_token:
        return None
    hashed_token = token_hash(raw_token)
    if hashed_token is None:
        return None
    return CampaignRecipient.objects.filter(tracking_token_hash=hashed_token).values_list("pk", flat=True).first()


def _enqueue_tracking_event(payload):
    from mailing import tasks  # noqa: PLC0415 - tasks imports services; avoid the cycle

    try:
        tasks.process_tracking_event.enqueue(payload)
    except DatabaseError:
        # The recipient still gets the pixel/redirect; the miss is logged loudly.
        logger.exception(
            "tracking_event_enqueue_failed kind=%s recipient_id=%s", payload["kind"], payload["recipient_id"]
        )
        return False
    return True


def record_open(raw_token):
    """Web path for the open pixel: validate the token, enqueue, return. Never locks."""
    recipient_id = _recipient_id_for_tracking_token(raw_token)
    if recipient_id is None:
        return None
    _enqueue_tracking_event({"kind": "open", "recipient_id": recipient_id, "occurred_at": timezone.now().isoformat()})
    return recipient_id


def record_click(raw_token, destination_url):
    """Web path for the click redirect: validate the token, enqueue, return. Never locks."""
    if not is_allowed_click_destination(destination_url):
        return None
    recipient_id = _recipient_id_for_tracking_token(raw_token)
    if recipient_id is None:
        return None
    _enqueue_tracking_event(
        {
            "kind": "click",
            "recipient_id": recipient_id,
            "occurred_at": timezone.now().isoformat(),
            "url": destination_url,
        }
    )
    return recipient_id


def apply_tracking_event(payload):
    """Worker side: persist one open/click. Recipient row lock only, counters after commit."""
    kind = payload["kind"]
    if kind not in {"open", "click"}:
        raise ValueError(f"unknown tracking event kind {kind!r}")
    occurred_at = datetime.fromisoformat(payload["occurred_at"])
    with transaction.atomic():
        recipient = (
            CampaignRecipient.objects.select_for_update(of=("self",))
            .select_related("campaign", "campaign__client", "campaign__audience", "contact")
            .filter(pk=payload["recipient_id"])
            .first()
        )
        if recipient is None:
            return None
        if kind == "open":
            update_fields = ["open_count", "updated_at"]
            recipient.open_count += 1
            if recipient.first_opened_at is None:
                recipient.first_opened_at = occurred_at
                update_fields.append("first_opened_at")
            recipient.save(update_fields=update_fields)
            event = _create_campaign_event(recipient, EmailEventType.OPEN)
        else:
            update_fields = ["click_count", "updated_at"]
            recipient.click_count += 1
            if recipient.first_clicked_at is None:
                recipient.first_clicked_at = occurred_at
                update_fields.append("first_clicked_at")
            recipient.save(update_fields=update_fields)
            event = _create_campaign_event(recipient, EmailEventType.CLICK, url=payload.get("url", ""))
        emit_cmp_contact_event(event)
        emit_client_callback(event)
    refresh_campaign_engagement_counts_if_due(recipient.campaign_id)
    return event


class UnsubscribeOutcome:
    APPLIED = "applied"
    DEFERRED = "deferred"


def apply_unsubscribe(raw_token):
    """Unsubscribe the contact from the single audience (list) the email was sent to.

    Durable and bounded: the suppression is written synchronously with the
    recipient row lock only and a short lock_timeout. If that lock cannot be
    had in time (e.g. a sender is mid-SES-call for this exact recipient), the
    unsubscribe is enqueued as a durable task instead of making the recipient
    wait, and the error is logged. Returns (recipient, outcome) or (None, None).
    """
    recipient = get_recipient_by_unsubscribe_token(raw_token)
    if recipient is None:
        return None, None
    occurred_at = timezone.now()
    try:
        apply_unsubscribe_for_recipient(
            recipient.pk, occurred_at, lock_timeout_ms=settings.RELAY_UNSUBSCRIBE_LOCK_TIMEOUT_MS
        )
    except OperationalError:
        logger.exception("unsubscribe_deferred recipient_id=%s reason=sync_write_failed", recipient.pk)
        from mailing import tasks  # noqa: PLC0415 - tasks imports services; avoid the cycle

        # If this enqueue also fails the exception propagates: the view shows an
        # error and nothing pretends the unsubscribe was recorded.
        tasks.apply_deferred_unsubscribe.enqueue(
            {"recipient_id": recipient.pk, "occurred_at": occurred_at.isoformat()}
        )
        return recipient, UnsubscribeOutcome.DEFERRED
    return recipient, UnsubscribeOutcome.APPLIED


def apply_unsubscribe_for_recipient(recipient_id, occurred_at, *, lock_timeout_ms=None):
    with transaction.atomic():
        if lock_timeout_ms and connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('lock_timeout', %s, true)", [f"{int(lock_timeout_ms)}ms"])
        recipient = (
            CampaignRecipient.objects.select_for_update(of=("self",))
            .select_related("campaign", "campaign__client", "campaign__audience", "contact")
            .filter(pk=recipient_id)
            .first()
        )
        if recipient is None:
            return None
        campaign = recipient.campaign

        unsubscribe_contact(
            recipient.contact, campaign.audience, reason="public_unsubscribe", unsubscribed_at=occurred_at
        )

        if recipient.status != CampaignRecipientStatus.UNSUBSCRIBED:
            recipient.status = CampaignRecipientStatus.UNSUBSCRIBED
            recipient.save(update_fields=["status", "updated_at"])

        event = _create_campaign_event(
            recipient,
            EmailEventType.UNSUBSCRIBE,
            metadata={"scope": "audience"},
        )
        emit_cmp_contact_event(event)
        emit_client_callback(event)
        transaction.on_commit(lambda: _enqueue_engagement_refresh(campaign.pk))
    return recipient


def _enqueue_engagement_refresh(campaign_id):
    from mailing import tasks  # noqa: PLC0415 - tasks imports services; avoid the cycle

    try:
        tasks.refresh_campaign_engagement.enqueue(campaign_id)
    except DatabaseError:
        logger.exception("engagement_refresh_enqueue_failed campaign_id=%s", campaign_id)


def refresh_campaign_engagement_counts_if_due(campaign_id):
    """Refresh at most once per interval per process, plus once the tracking queue drains.

    Same contract as refresh_campaign_provider_counts_if_due: counters are a
    pure function of recipient rows, so skipped refreshes lose nothing and the
    event that finds no tracking task READY makes the totals exact.
    """
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415

    due = cache.add(
        f"campaign-engagement-counts:{campaign_id}", 1, timeout=ENGAGEMENT_COUNTS_REFRESH_INTERVAL_SECONDS
    )
    if not due and DBTaskResult.objects.filter(queue_name=TRACKING_QUEUE, status="READY").exists():
        return False
    refresh_campaign_engagement_counts(campaign_id)
    return True


@transaction.atomic
def apply_resubscribe(raw_token):
    """Undo a public unsubscribe for the same single audience (list)."""
    recipient = get_recipient_by_unsubscribe_token(raw_token)
    if recipient is None:
        return None
    campaign = recipient.campaign
    subscribe_contact(recipient.contact, campaign.audience)
    event = _create_campaign_event(recipient, EmailEventType.SUBSCRIBE, metadata={"scope": "audience"})
    emit_cmp_contact_event(event)
    return recipient


def refresh_campaign_engagement_counts(campaign):
    # One pass over the campaign's recipients, written with a single UPDATE so
    # the campaign row is locked only for that statement.
    campaign_id = getattr(campaign, "pk", campaign)
    totals = CampaignRecipient.objects.filter(campaign_id=campaign_id).aggregate(
        open_count=Sum("open_count"),
        click_count=Sum("click_count"),
        unique_open_count=Count("pk", filter=Q(first_opened_at__isnull=False)),
        unique_click_count=Count("pk", filter=Q(first_clicked_at__isnull=False)),
        unsubscribe_count=Count("pk", filter=Q(status=CampaignRecipientStatus.UNSUBSCRIBED)),
    )
    counts = {
        "unique_open_count": totals["unique_open_count"],
        "open_count": totals["open_count"] or 0,
        "unique_click_count": totals["unique_click_count"],
        "click_count": totals["click_count"] or 0,
        "unsubscribe_count": totals["unsubscribe_count"],
    }
    Campaign.objects.filter(pk=campaign_id).update(**counts, updated_at=timezone.now())
    return counts


def _create_campaign_event(recipient, event_type, *, url="", metadata=None):
    return EmailEvent.objects.create(
        campaign=recipient.campaign,
        campaign_recipient=recipient,
        contact=recipient.contact,
        client=recipient.campaign.client,
        audience=recipient.campaign.audience,
        event_type=event_type,
        url=url,
        metadata=metadata or {},
    )
