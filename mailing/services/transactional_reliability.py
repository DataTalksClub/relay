"""Bounded send failure handling and read-only queued-age monitoring."""

import json
import logging
from datetime import timedelta
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from mailing.models import TransactionalMessage, TransactionalMessageStatus
from mailing.services.transactional_sender import TERMINAL_ACK_STATUSES, _mark_failed

logger = logging.getLogger(__name__)
_last_alert_at = None


def fail_exhausted_transactional_message(payload, attempt, error):
    """Stop retrying without releasing an uncertain/in-progress SES claim.

    A failure after SES may leave SENDING. Marking it failed keeps subsequent
    task deliveries from sending again; operators must reconcile provider
    evidence before manually releasing such a message.
    """
    with transaction.atomic():
        message = TransactionalMessage.objects.select_for_update().filter(
            pk=payload["transactional_message_id"],
            client_id=payload["client_id"],
            idempotency_key=payload["idempotency_key"],
        ).first()
        if message is None or message.status in TERMINAL_ACK_STATUSES:
            return
        uncertain = message.status == TransactionalMessageStatus.SENDING
        _mark_failed(
            message.pk,
            f"Transactional send exhausted {attempt} attempts ({type(error).__name__})"
            + ("; delivery outcome uncertain, reconcile SES before any manual retry" if uncertain else ""),
            metadata={"reason": "retry_exhausted", "attempts": attempt, "delivery_uncertain": uncertain},
        )
        logger.error("transactional_send_retry_exhausted message_id=%s attempts=%s delivery_uncertain=%s",
                     message.pk, attempt, uncertain)


def alert_stuck_transactional_messages(*, now=None):
    """Alert on old queued rows; never automatically redrive historical mail.

    Run by the scheduler every pass, with a per-process repeat interval. A
    scheduler restart deliberately permits an immediate alert. Logs/webhooks
    contain counts, numeric IDs and age, never recipient addresses or bodies.
    """
    global _last_alert_at
    now = now or timezone.now()
    if _last_alert_at is not None and now - _last_alert_at < timedelta(
        seconds=settings.RELAY_TRANSACTIONAL_ALERT_REPEAT_SECONDS,
    ):
        return 0
    cutoff = now - timedelta(minutes=settings.RELAY_TRANSACTIONAL_QUEUED_ALERT_MINUTES)
    queued = TransactionalMessage.objects.filter(
        status=TransactionalMessageStatus.QUEUED, created_at__lt=cutoff,
    ).order_by("created_at", "pk")
    count = queued.count()
    if not count:
        return 0
    oldest = queued.values("pk", "created_at").first()
    if oldest is None:
        # Delivery workers may drain the final queued row after the count.
        return 0
    alert = {
        "event": "relay_transactional_queued_too_long",
        "count": count,
        "threshold_minutes": settings.RELAY_TRANSACTIONAL_QUEUED_ALERT_MINUTES,
        "oldest_age_seconds": int((now - oldest["created_at"]).total_seconds()),
        "message_ids": list(queued.values_list("pk", flat=True)[:50]),
    }
    logger.error("%s", json.dumps(alert, sort_keys=True))
    _last_alert_at = now
    if settings.RELAY_TRANSACTIONAL_ALERT_WEBHOOK_URL:
        try:
            request = Request(
                settings.RELAY_TRANSACTIONAL_ALERT_WEBHOOK_URL,
                data=json.dumps(alert).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                response.read(1)
        except Exception as exc:
            # URLs may contain credentials; never log the URL/exception text.
            logger.error("transactional_queued_alert_webhook_failed error_type=%s", type(exc).__name__)
    return count
