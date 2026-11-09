"""Outcomes the sender could not record: lease expiry, SES-event reconciliation, operator resend.

A recipient is ``SENDING`` while a sender owns it. If the sender never records the SES
result (crash, SIGKILL, deploy), the claim goes stale and the periodic ``expire_stale_claims``
turns it ``UNKNOWN``. ``UNKNOWN`` is never auto-resent: the message may well be delivered.
It leaves that state only by (a) a matching SES event, which finalizes it as SENT, or
(b) an operator explicitly confirming a resend.
"""

import hashlib
import logging
import uuid
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from mailing.models import (
    CampaignRecipient,
    CampaignRecipientStatus,
    EmailEvent,
    EmailEventType,
    TransactionalMessage,
)
from mailing.services.api_errors import ApiValidationError

logger = logging.getLogger(__name__)

# An SES event's mail timestamp is the send time: after the claim, within the lease plus slack.
EVENT_BEFORE_CLAIM_SLACK = timedelta(seconds=120)
EVENT_AFTER_CLAIM_SLACK = timedelta(minutes=5)
MATCHABLE_EVENT_TYPES = ("send", "delivery", "delivery_delay", "bounce", "complaint", "open", "click")
UNKNOWN_LIST_LIMIT = 500


class ResendNotConfirmed(ApiValidationError):
    def __init__(self):
        super().__init__({"confirm": "resend of UNKNOWN recipients requires explicit confirmation (confirm=true)"})

    def __str__(self):
        return "resend requires explicit confirm=true"


def recipient_hash(email):
    """What the SES webhook stores instead of the destination address."""
    return hashlib.sha256((email or "").strip().lower().encode("utf-8")).hexdigest()


def expire_stale_claims(*, now=None, lease_seconds=None):
    """SENDING past its lease -> UNKNOWN (loudly). Returns the number of rows moved."""
    from mailing.services.campaign_sender import refresh_campaign_send_counts  # noqa: PLC0415 - import cycle

    now = now or timezone.now()
    lease = int(lease_seconds if lease_seconds is not None else settings.CAMPAIGN_SEND_LEASE_SECONDS)
    stale = CampaignRecipient.objects.filter(
        status=CampaignRecipientStatus.SENDING, claimed_at__lt=now - timedelta(seconds=lease)
    )
    per_campaign = dict(stale.values_list("campaign_id").annotate(n=Count("id")))
    if not per_campaign:
        return 0
    moved = stale.update(
        status=CampaignRecipientStatus.UNKNOWN, last_error="send_outcome_unknown:lease_expired", updated_at=now
    )
    for campaign_id, count in per_campaign.items():
        logger.error(
            "campaign_claims_expired campaign_id=%s rows=%d lease_s=%d: send outcome unknown, "
            "NOT resending; reconcile from SES events or resend from the operator list",
            campaign_id, count, lease,
        )
        refresh_campaign_send_counts(campaign_id)
    return moved


def _event_time(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return parse_datetime(value)
    return None


def reconcile_unknown_from_event(event):
    """Finalize one UNKNOWN recipient as SENT from an SES event; True exactly once.

    ``event``: ``ses_message_id`` and ``recipient`` (address) are required; ``campaign_id``
    and ``mail_timestamp`` narrow the match. Anything ambiguous (several UNKNOWN rows for
    that address, a message id that already belongs to another message) is left alone for
    the operator.
    """
    message_id = event.get("ses_message_id") or ""
    email = (event.get("recipient") or "").strip()
    if not message_id or not email:
        return False
    occurred_at = _event_time(event.get("mail_timestamp"))
    with transaction.atomic():
        candidates = CampaignRecipient.objects.select_for_update(of=("self",)).filter(
            status=CampaignRecipientStatus.UNKNOWN, email__iexact=email
        )
        if event.get("campaign_id"):
            candidates = candidates.filter(campaign_id=event["campaign_id"])
        rows = list(candidates[:3])
        if occurred_at is not None:
            lease = timedelta(seconds=int(settings.CAMPAIGN_SEND_LEASE_SECONDS))
            rows = [
                row for row in rows
                if row.claimed_at is None
                or row.claimed_at - EVENT_BEFORE_CLAIM_SLACK <= occurred_at <= row.claimed_at + lease + EVENT_AFTER_CLAIM_SLACK
            ]
        if len(rows) > 1:
            logger.warning(
                "send_reconcile_ambiguous ses_message_id=%s candidates=%d: left for the operator",
                message_id, len(rows),
            )
            return False
        if not rows:
            return False
        if (CampaignRecipient.objects.filter(ses_message_id=message_id).exists()
                or TransactionalMessage.objects.filter(ses_message_id=message_id).exists()):
            return False
        row = rows[0]
        now = timezone.now()
        updated = CampaignRecipient.objects.filter(pk=row.pk, status=CampaignRecipientStatus.UNKNOWN).update(
            status=CampaignRecipientStatus.SENT, ses_message_id=message_id,
            sent_at=occurred_at or row.claimed_at or now, last_error="", updated_at=now,
        )
        if not updated:
            return False
        EmailEvent.objects.create(
            campaign_id=row.campaign_id, campaign_recipient=row, contact_id=row.contact_id,
            client_id=row.campaign.client_id, audience_id=row.campaign.audience_id,
            event_type=EmailEventType.SENT, metadata={"ses_message_id": message_id, "reconciled_from_unknown": True},
        )
    logger.warning(
        "send_reconciled_unknown campaign_id=%s recipient_id=%s ses_message_id=%s", row.campaign_id, row.pk, message_id,
    )
    return True


def reconcile_unknown_recipients(*, limit=200):
    """Match stored, uncorrelated SES events to UNKNOWN rows (periodic). Returns rows finalized."""
    from mailing.services.campaign_sender import refresh_campaign_send_counts  # noqa: PLC0415 - import cycle
    from mailing.services.ses_webhooks import reconcile_uncorrelated_ses_events  # noqa: PLC0415 - import cycle

    unknown = list(
        CampaignRecipient.objects.filter(status=CampaignRecipientStatus.UNKNOWN)
        .order_by("claimed_at").values_list("email", "claimed_at")[:UNKNOWN_LIST_LIMIT]
    )
    if not unknown:
        return 0
    by_hash = {recipient_hash(email): email for email, _ in unknown}
    oldest = min((claimed for _, claimed in unknown if claimed), default=timezone.now())
    events = (
        EmailEvent.objects.filter(
            campaign_recipient__isnull=True, transactional_message__isnull=True,
            created_at__gte=oldest - timedelta(hours=1),
            metadata__mail_destination_hash__in=list(by_hash),
            metadata__notification_type__in=MATCHABLE_EVENT_TYPES,
        ).exclude(provider_event_id="").order_by("id")[:limit]
    )
    finalized = 0
    for event in events:
        meta = event.metadata
        matched = reconcile_unknown_from_event({
            "ses_message_id": meta.get("ses_message_id", ""),
            "recipient": by_hash.get(meta.get("mail_destination_hash", ""), ""),
            "mail_timestamp": meta.get("mail_timestamp"),
        })
        finalized += bool(matched)
    if finalized:
        reconcile_uncorrelated_ses_events()
        for campaign_id in set(
            CampaignRecipient.objects.filter(
                status=CampaignRecipientStatus.SENT, last_error="", updated_at__gte=timezone.now() - timedelta(minutes=5)
            ).values_list("campaign_id", flat=True)
        ):
            refresh_campaign_send_counts(campaign_id)
    return finalized


def unknown_recipients(campaign_id, *, limit=UNKNOWN_LIST_LIMIT):
    rows = CampaignRecipient.objects.filter(campaign_id=campaign_id, status=CampaignRecipientStatus.UNKNOWN)
    return {
        "campaign_id": campaign_id,
        "count": rows.count(),
        "recipients": [
            {"id": r.id, "email": r.email, "claimed_at": r.claimed_at.isoformat() if r.claimed_at else None,
             "last_error": r.last_error}
            for r in rows.order_by("claimed_at", "id")[:limit]
        ],
    }


def unknown_count(campaign_id):
    return CampaignRecipient.objects.filter(campaign_id=campaign_id, status=CampaignRecipientStatus.UNKNOWN).count()


def resend_unknown(campaign_id, recipient_ids, *, confirm, actor):
    """Operator action: UNKNOWN -> PENDING and enqueue. Requires ``confirm is True``.

    This can deliver a second copy to someone who already got the first; that is why it is
    explicit, logged, and per selected row. The tokens are re-minted at the next claim, so
    links in a first copy that did arrive stop working.
    """
    if confirm is not True:
        raise ResendNotConfirmed()
    if not actor:
        raise ApiValidationError({"actor": "resend requires an identified operator"})
    ids = sorted({int(i) for i in recipient_ids})
    if not ids:
        raise ApiValidationError({"recipient_ids": "select at least one UNKNOWN recipient"})
    from mailing.enqueue import enqueue_campaign_email  # noqa: PLC0415 - import cycle

    now = timezone.now()
    with transaction.atomic():
        rows = list(
            CampaignRecipient.objects.select_for_update(of=("self",)).select_related("campaign").filter(
                campaign_id=campaign_id, pk__in=ids, status=CampaignRecipientStatus.UNKNOWN
            )
        )
        if not rows:
            return 0
        row_ids = [row.pk for row in rows]
        CampaignRecipient.objects.filter(pk__in=row_ids).update(
            status=CampaignRecipientStatus.PENDING, claimed_at=None, claim_owner="",
            tracking_token_hash=None, unsubscribe_token_hash=None, last_error="operator_resend_unknown", updated_at=now,
        )
        EmailEvent.objects.bulk_create([
            EmailEvent(
                campaign_id=campaign_id, campaign_recipient=row, contact_id=row.contact_id,
                client_id=row.campaign.client_id, audience_id=row.campaign.audience_id,
                event_type=EmailEventType.QUEUED,
                metadata={"action": "operator_resend_unknown", "actor": str(actor)[:255]},
            )
            for row in rows
        ])
        batch_id = f"campaign-{campaign_id}-resend-{uuid.uuid4().hex[:8]}"
        payload = {
            "contract": "campaign-email", "version": 1, "campaign_id": campaign_id, "batch_id": batch_id,
            "idempotency_key": batch_id, "campaign_recipient_ids": row_ids,
        }
        transaction.on_commit(lambda: enqueue_campaign_email(payload))
    logger.warning(
        "campaign_unknown_resend campaign_id=%s actor=%s recipients=%d batch_id=%s",
        campaign_id, actor, len(row_ids), batch_id,
    )
    return len(row_ids)
