"""Generic tenant-scoped client callbacks for delivery and engagement events.

Every client-visible transport transition appends an ``EmailEvent``; this
module turns exactly those transitions into durable, versioned, redacted
callback work. One ``ClientCallback`` outbox row is created inside the same
transaction that commits the transition, and a polling dispatcher signs and
posts it after commit. The row is deduplicated on ``(client, event_id)``, so
repeated transition processing or duplicate provider events never create
duplicate callback work.

The wire contract is documented in ``docs/api.md``:

- the body is the canonical JSON event with an explicit ``contract_version``;
- requests are signed with HMAC-SHA-256 over ``<timestamp>.<raw-body>`` using
  the client's dedicated callback signing secret (never a Bearer credential);
- retries use bounded exponential backoff for transport errors, ``429`` and
  ``5xx``; other ``4xx`` and redirects are terminal; and
- payloads carry stable identifiers, timestamps, and safe reason codes only --
  never recipient addresses, message content, credentials, or raw provider
  data.
"""

import hashlib
import hmac
import json
import logging
import socket
import urllib.error
import urllib.request
import uuid
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from mailing.models import (
    CallbackEndpoint,
    ClientCallback,
    ClientCallbackStatus,
    EmailEventType,
)

logger = logging.getLogger(__name__)

SIGNATURE_PREFIX = "sha256="
HTTP_REDIRECT_CLASSES = {301, 302, 303, 307, 308}
HTTP_RATE_LIMIT_STATUS = 429

CALLBACK_EVENT_TYPES = {
    EmailEventType.QUEUED: "delivery.accepted",
    EmailEventType.SENT: "delivery.accepted",
    EmailEventType.SKIPPED: "delivery.suppressed",
    EmailEventType.DELIVERED: "delivery.delivered",
    EmailEventType.BOUNCE: "delivery.bounced",
    EmailEventType.COMPLAINT: "delivery.complained",
    EmailEventType.OPEN: "engagement.opened",
    EmailEventType.CLICK: "engagement.clicked",
    EmailEventType.SUBSCRIBE: "subscription.changed",
    EmailEventType.UNSUBSCRIBE: "subscription.changed",
}

# The only metadata values safe enough to become a reason code. Anything else
# collapses to a generic code so provider diagnostics never leak to clients.
SUPPRESSION_REASON_CODES = {
    "unverified",
    "invalid_email",
    "global_unsubscribe",
    "client_unsubscribe",
    "audience_unsubscribe",
    "hard_bounce",
    "complaint",
    "duplicate",
    "suppressed",
    "category_unsubscribe",
    "missing_category_scope",
}

MESSAGE_KIND_TRANSACTIONAL = "transactional"
MESSAGE_KIND_CAMPAIGN = "campaign"


def callback_event_id(event):
    """Stable event id for one transition: the same on every reprocessing."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"urn:relay:email-event:{event.pk}")


def canonical_callback_body(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def callback_signature(secret, timestamp, body):
    """Sign ``<timestamp>.<raw-body>`` exactly as webhook tasks are signed."""
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


def signed_callback_headers(endpoint, callback, *, attempt):
    timestamp = str(int(timezone.now().timestamp()))
    signature = callback_signature(endpoint.signing_secret, timestamp, callback.body.encode("utf-8"))
    return {
        "Content-Type": "application/json",
        "User-Agent": "relay/1",
        "X-Relay-Timestamp": timestamp,
        "X-Relay-Signature": signature,
        "X-Relay-Event-Id": str(callback.event_id),
        "X-Relay-Event-Type": callback.event_type,
        "X-Relay-Contract-Version": str(callback.contract_version),
        "X-Relay-Attempt": str(attempt),
    }


def build_callback_payload(event, endpoint, *, sequence):
    """Build the redacted, versioned event payload for one transition.

    Returns ``None`` when the transition has no client-visible callback (no
    owning client, unmapped event type). The payload carries identifiers,
    timestamps, and safe reason codes only; redaction canaries (recipient
    address, subject, body, context, credentials, raw provider data) never
    enter it.
    """
    event_type = CALLBACK_EVENT_TYPES.get(event.event_type)
    if event_type is None:
        return None

    message_kind, message_ref, template_key, client_reference = message_identity(event)
    payload = {
        "contract_version": endpoint.contract_version,
        "event_id": str(callback_event_id(event)),
        "event_type": event_type,
        "timestamp": event.created_at.isoformat(),
        "sequence": sequence,
        "message_id": message_ref or None,
        "client_reference": client_reference or None,
        "template_key": template_key,
    }
    if event_type == "delivery.bounced":
        payload["bounce_type"] = "hard" if is_hard_bounce(event.metadata or {}) else "soft"
    reason = safe_reason_code(event)
    if reason:
        payload["reason_code"] = reason
    return payload


def message_identity(event):
    if event.transactional_message_id and event.transactional_message is not None:
        return (
            MESSAGE_KIND_TRANSACTIONAL,
            str(event.transactional_message_id),
            event.transactional_message.template_key,
            event.transactional_message.idempotency_key,
        )
    if event.transactional_message_id:
        return MESSAGE_KIND_TRANSACTIONAL, str(event.transactional_message_id), "", ""
    if event.campaign_recipient_id:
        return MESSAGE_KIND_CAMPAIGN, str(event.campaign_recipient_id), "", ""
    return "", "", "", ""


def safe_reason_code(event):
    metadata = event.metadata or {}
    if event.event_type == EmailEventType.BOUNCE:
        return "hard_bounce" if is_hard_bounce(metadata) else "soft_bounce"
    if event.event_type == EmailEventType.COMPLAINT:
        return "complaint"
    if event.event_type == EmailEventType.SKIPPED:
        reason = str(metadata.get("reason", ""))
        return reason if reason in SUPPRESSION_REASON_CODES else "suppressed"
    if event.event_type == EmailEventType.SUBSCRIBE:
        return "subscribed"
    if event.event_type == EmailEventType.UNSUBSCRIBE:
        return "unsubscribed"
    return ""


def is_hard_bounce(metadata):
    bounce_type = (metadata.get("bounce_type") or "").casefold()
    bounce_sub_type = (metadata.get("bounce_sub_type") or "").casefold()
    return bounce_type == "permanent" or bounce_sub_type in {"general", "suppressed", "onaccountsuppressionlist"}


def next_callback_sequence(client, message_kind, message_ref):
    if not message_kind:
        return ClientCallback.objects.filter(client=client, message_kind="").count() + 1
    return (
        ClientCallback.objects.filter(
            client=client,
            message_kind=message_kind,
            message_ref=message_ref,
        ).count()
        + 1
    )


def emit_client_callback(event):
    """Create the durable callback work for one committed-pending transition.

    Call inside the transaction that appends the ``EmailEvent`` so the callback
    row commits atomically with the state it announces. Deduplicated on
    ``(client, event_id)``: repeated transition processing returns the existing
    row instead of creating a second one.
    """
    client = event.client
    if client is None or event.event_type not in CALLBACK_EVENT_TYPES:
        return None
    if event.campaign_recipient_id is not None:
        # Campaign-scoped transitions stay on the CMP channel. Client
        # callbacks carry transactional deliveries and client-level
        # subscription changes, the transitions whose receivers own a
        # client_reference.
        return None
    endpoint = CallbackEndpoint.objects.filter(client=client, enabled=True).first()
    if endpoint is None:
        return None

    message_kind, message_ref, _, _ = message_identity(event)
    payload = build_callback_payload(
        event,
        endpoint,
        sequence=next_callback_sequence(client, message_kind, message_ref),
    )
    if payload is None:
        return None
    body = canonical_callback_body(payload)
    callback, _ = ClientCallback.objects.get_or_create(
        client=client,
        event_id=uuid.UUID(payload["event_id"]),
        defaults={
            "endpoint": endpoint,
            "email_event": event,
            "transactional_message_id": event.transactional_message_id,
            "campaign_recipient_id": event.campaign_recipient_id,
            "event_type": payload["event_type"],
            "contract_version": payload["contract_version"],
            "client_reference": payload["client_reference"] or "",
            "message_kind": message_kind,
            "message_ref": message_ref,
            "template_key": payload["template_key"],
            "sequence": payload["sequence"],
            "payload": payload,
            "body": body.decode("utf-8"),
            "body_hash": hashlib.sha256(body).hexdigest(),
            "max_attempts": settings.CLIENT_CALLBACK_MAX_ATTEMPTS,
            "next_attempt_at": timezone.now(),
        },
    )
    return callback


def due_client_callbacks(*, limit=25, now=None):
    now = now or timezone.now()
    return (
        ClientCallback.objects.select_related("client", "endpoint")
        .filter(status=ClientCallbackStatus.PENDING, next_attempt_at__lte=now)
        .order_by("next_attempt_at", "id")[:limit]
    )


def process_due_client_callbacks(*, limit=25, now=None):
    processed = 0
    delivered = 0
    failed = 0
    for callback in due_client_callbacks(limit=limit, now=now):
        processed += 1
        if dispatch_client_callback(callback):
            delivered += 1
        else:
            failed += 1
    return {"processed": processed, "delivered": delivered, "failed": failed}


def dispatch_client_callback(callback, *, now=None):
    """Deliver one callback attempt. Never touches transport state."""
    # Re-read the endpoint: enable/disable may have changed since the callback
    # row was created or last attempted.
    endpoint = CallbackEndpoint.objects.filter(pk=callback.endpoint_id, enabled=True).first()
    if endpoint is None:
        mark_callback_failed(callback, "endpoint_disabled", "Callback endpoint is disabled.")
        return False

    try:
        post_callback(endpoint, callback)
    except RetryableCallbackError as exc:
        logger.warning("client callback retry event_id=%s code=%s", callback.event_id, exc.code)
        mark_callback_retry(callback, exc.code, str(exc), response_status_class=exc.status_class, now=now)
        return False
    except PermanentCallbackError as exc:
        mark_callback_failed(callback, exc.code, str(exc), response_status_class=exc.status_class)
        return False

    mark_callback_delivered(callback, now=now)
    return True


def post_callback(endpoint, callback):
    """POST the canonical body. Returns the 2xx status or raises."""
    body = callback.body.encode("utf-8")
    headers = signed_callback_headers(endpoint, callback, attempt=callback.attempt_count + 1)
    request = urllib.request.Request(endpoint.url, data=body, headers=headers, method="POST")
    timeout = settings.CLIENT_CALLBACK_TIMEOUT_SECONDS
    try:
        with NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in HTTP_REDIRECT_CLASSES:
            raise PermanentCallbackError(
                "callback endpoint redirected; redirects are not followed",
                code="redirect_not_allowed",
                status_class="3xx",
            ) from exc
        if exc.code >= 500 or exc.code == HTTP_RATE_LIMIT_STATUS:
            code = "rate_limited" if exc.code == HTTP_RATE_LIMIT_STATUS else "http_5xx"
            raise RetryableCallbackError(
                f"callback endpoint returned HTTP {exc.code}", code=code, status_class="5xx" if exc.code >= 500 else "4xx"
            ) from exc
        raise PermanentCallbackError(
            f"callback endpoint returned HTTP {exc.code}", code="http_4xx", status_class="4xx"
        ) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        code = "timeout" if isinstance(exc, (TimeoutError, socket.timeout)) else "connection_error"
        raise RetryableCallbackError(f"callback delivery failed: {exc}", code=code, status_class="") from exc

    if not 200 <= status < 300:
        raise RetryableCallbackError(
            f"callback endpoint returned HTTP {status}", code="http_5xx", status_class="5xx"
        )
    return status


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


class RetryableCallbackError(Exception):
    def __init__(self, message, *, code, status_class=""):
        self.code = code
        self.status_class = status_class
        super().__init__(message)


class PermanentCallbackError(Exception):
    def __init__(self, message, *, code, status_class=""):
        self.code = code
        self.status_class = status_class
        super().__init__(message)


def mark_callback_delivered(callback, *, now=None):
    now = now or timezone.now()
    callback.status = ClientCallbackStatus.DELIVERED
    callback.attempt_count += 1
    callback.last_attempt_at = now
    callback.delivered_at = now
    callback.response_status_class = "2xx"
    callback.last_error_code = ""
    callback.last_error = ""
    callback.save(
        update_fields=[
            "status",
            "attempt_count",
            "last_attempt_at",
            "delivered_at",
            "response_status_class",
            "last_error_code",
            "last_error",
            "updated_at",
        ]
    )


def mark_callback_retry(callback, error_code, error, *, response_status_class="", now=None):
    now = now or timezone.now()
    callback.attempt_count += 1
    callback.last_attempt_at = now
    callback.response_status_class = response_status_class
    callback.last_error_code = error_code
    callback.last_error = error[:500]
    if callback.attempt_count >= callback.max_attempts:
        callback.status = ClientCallbackStatus.FAILED
    else:
        callback.next_attempt_at = now + retry_delay(callback)
    callback.save(
        update_fields=[
            "status",
            "attempt_count",
            "next_attempt_at",
            "last_attempt_at",
            "response_status_class",
            "last_error_code",
            "last_error",
            "updated_at",
        ]
    )


def mark_callback_failed(callback, error_code, error, *, response_status_class=""):
    now = timezone.now()
    callback.status = ClientCallbackStatus.FAILED
    callback.attempt_count += 1
    callback.last_attempt_at = now
    callback.response_status_class = response_status_class
    callback.last_error_code = error_code
    callback.last_error = error[:500]
    callback.save(
        update_fields=[
            "status",
            "attempt_count",
            "last_attempt_at",
            "response_status_class",
            "last_error_code",
            "last_error",
            "updated_at",
        ]
    )


def retry_delay(callback):
    """Bounded exponential backoff with jitter deterministic in the callback.

    ``base * 2^(attempt-1)`` grows by a quarter of itself at most, with the
    fraction derived from the stable event id and attempt number, so the
    schedule is fully determined by stored data.
    """
    attempt = max(callback.attempt_count, 1)
    base = settings.CLIENT_CALLBACK_RETRY_BASE_SECONDS
    cap = settings.CLIENT_CALLBACK_RETRY_MAX_DELAY_SECONDS
    stall = int(hashlib.sha256(f"{callback.event_id}:{attempt}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    delay = base * (2 ** (attempt - 1))
    delay = min(delay * (1 + 0.25 * stall), cap)
    return timedelta(seconds=delay)


def callback_backlog_status():
    """Safe operator metrics: backlog, oldest age, recent failures. No PII."""
    now = timezone.now()
    pending = ClientCallback.objects.filter(status=ClientCallbackStatus.PENDING)
    oldest = pending.order_by("created_at").values_list("created_at", flat=True).first()
    return {
        "pending": pending.count(),
        "oldest_pending_age_s": int((now - oldest).total_seconds()) if oldest else 0,
        "failed_24h": ClientCallback.objects.filter(
            status=ClientCallbackStatus.FAILED,
            last_attempt_at__gte=now - timedelta(hours=24),
        ).count(),
    }
