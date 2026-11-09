import base64
import hashlib
import http.client
import json
import logging
import re
import urllib.error
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.hashes import SHA1, SHA256
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django_tasks_db.models import DBTaskResult

from mailing.enqueue import enqueue_ses_webhook
from mailing.models import (
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    Contact,
    EmailEvent,
    EmailEventType,
    OperatorAudit,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.queue_contracts import CONTRACT_VERSION, SES_WEBHOOKS_CONTRACT, validate_ses_webhook_message
from mailing.services.client_callbacks import emit_client_callback, is_hard_bounce
from mailing.services.cmp_callbacks import emit_cmp_contact_event
from mailing.services.operator_management import safe_metadata
from mailing.sqs import sanitize_for_logging

SNS_NOTIFICATION = "Notification"
SNS_SUBSCRIPTION_CONFIRMATION = "SubscriptionConfirmation"
SUPPORTED_SNS_TYPES = {SNS_NOTIFICATION, SNS_SUBSCRIPTION_CONFIRMATION}
SNS_MOCK_SIGNATURE = "datamailer-local-sns-signature"

SES_EVENT_TYPES = {
    "Send": "send",
    "Reject": "reject",
    "Delivery": "delivery",
    "Bounce": "bounce",
    "Complaint": "complaint",
    "Open": "open",
    "Click": "click",
    "DeliveryDelay": "delivery_delay",
}

# In-process cache of SNS signing certificates by URL. SNS rotates these
# rarely, so a second webhook with the same cert URL must not pay for another
# network fetch (F4: DoS amplifier on the public fallback endpoint).
# Bounded: keys are normalized (no query string) and only certs that
# verified a signature are stored, so hostile unique URLs cannot grow it.
_CERT_CACHE: dict = {}
_CERT_CACHE_MAX_ENTRIES = 16
_MAX_CERT_BYTES = 64 * 1024
_DEFAULT_CERT_CACHE_TTL_SECONDS = 3600.0
_SNS_CERT_HOST_RE = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")


def _cert_cache_ttl_seconds():
    # relay.settings coerces this with float_env already; fall back only if
    # an override supplies something unexpected.
    try:
        return float(getattr(settings, "SES_WEBHOOKS_CERT_CACHE_TTL_SECONDS", _DEFAULT_CERT_CACHE_TTL_SECONDS))
    except (TypeError, ValueError):
        return _DEFAULT_CERT_CACHE_TTL_SECONDS


def clear_signing_certificate_cache():
    _CERT_CACHE.clear()

EVENT_TYPE_BY_NOTIFICATION = {
    "send": EmailEventType.SENT,
    "reject": EmailEventType.FAILED,
    "delivery": EmailEventType.DELIVERED,
    "bounce": EmailEventType.BOUNCE,
    "complaint": EmailEventType.COMPLAINT,
    "open": EmailEventType.OPEN,
    "click": EmailEventType.CLICK,
}


class SesWebhookError(ValueError):
    status_code = 400


class SnsSignatureError(SesWebhookError):
    status_code = 403


logger = logging.getLogger(__name__)

_WARNED_EMPTY_TOPIC_ALLOWLIST = False


@dataclass(frozen=True)
class IngressResult:
    message_type: str
    enqueued: bool = False
    confirmed: bool = False


def ingest_sns_webhook(payload):
    if not isinstance(payload, dict):
        raise SesWebhookError("SNS payload must be a JSON object")

    message_type = payload.get("Type")
    if message_type not in SUPPORTED_SNS_TYPES:
        raise SesWebhookError("unsupported SNS message type")

    validate_sns_signature(payload)

    if message_type == SNS_SUBSCRIPTION_CONFIRMATION:
        confirmed = confirm_subscription(payload)
        return IngressResult(message_type=message_type, confirmed=confirmed)

    queue_payload = normalize_ses_notification(payload)
    enqueue_ses_webhook(queue_payload)
    return IngressResult(message_type=message_type, enqueued=True)


class SnsPayloadTooLargeError(SesWebhookError):
    status_code = 413


def check_sns_body_size(request):
    """Reject oversized SNS bodies before Django buffers them (F4).

    Checks the Content-Length header first so a hostile body is never read
    into memory. SNS always sends Content-Length; missing or unparseable
    values are rejected.
    """
    max_body_bytes = getattr(settings, "SES_WEBHOOKS_MAX_BODY_BYTES", 256 * 1024)
    try:
        content_length = int(request.META.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        content_length = 0
    if content_length <= 0 or content_length > max_body_bytes:
        raise SnsPayloadTooLargeError("SNS payload too large")
    return True


def validate_sns_signature(payload):
    mode = getattr(settings, "SES_WEBHOOKS_SIGNATURE_MODE", "strict")
    if mode == "disabled":
        return True
    if mode == "mock":
        # Test-only short-circuit: the TopicArn allowlist is enforced in
        # strict mode only. Never run mock/disabled outside tests.
        if payload.get("Signature") == SNS_MOCK_SIGNATURE:
            return True
        raise SnsSignatureError("invalid mock SNS signature")
    if mode != "strict":
        raise SnsSignatureError("unsupported SNS signature mode")

    cert_url = payload.get("SigningCertURL", "")
    signature = payload.get("Signature", "")
    version = payload.get("SignatureVersion", "")
    if not cert_url or not signature or version not in {"1", "2"}:
        raise SnsSignatureError("missing SNS signature fields")

    validate_topic_arn(payload)

    cert_bytes = fetch_signing_certificate(cert_url)
    try:
        cert = x509.load_pem_x509_certificate(cert_bytes)
    except Exception as exc:
        raise SnsSignatureError("invalid SNS signing certificate") from exc
    algorithm = SHA256() if version == "2" else SHA1()
    try:
        cert.public_key().verify(
            base64.b64decode(signature),
            canonical_sns_message(payload).encode("utf-8"),
            padding.PKCS1v15(),
            algorithm,
        )
    except Exception as exc:
        raise SnsSignatureError("invalid SNS signature") from exc
    # Cache only certs that actually verified a signature.
    _store_signing_certificate(cert_url, cert_bytes)
    return True


def fetch_signing_certificate(cert_url):
    """Return the PEM bytes for an SNS signing certificate URL.

    Serves previously verified certs from a small bounded in-process cache.
    Newly fetched certs are NOT stored here; validate_sns_signature stores
    them only after they verify a signature.
    """
    if not isinstance(cert_url, str) or not _is_allowed_sns_cert_url(cert_url):
        raise SnsSignatureError("invalid SNS signing certificate URL")
    key = _signing_certificate_cache_key(cert_url)
    now = monotonic()
    cached = _CERT_CACHE.get(key)
    if cached is not None:
        expires_at, cert_bytes = cached
        if now < expires_at:
            return cert_bytes
        _CERT_CACHE.pop(key, None)
    # Network failures (DNS, 404 cert path, connect/read timeouts, reset or
    # truncated responses) mean the signature cannot be checked, so they are
    # 403s, never 500s. URLError covers HTTPError/DNS/connect; TimeoutError
    # and HTTPException cover mid-read failures outside urlopen's wrapper.
    try:
        with urlopen(cert_url, timeout=5) as response:
            cert_bytes = response.read(_MAX_CERT_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as exc:
        raise SnsSignatureError("SNS signing certificate unreachable") from exc
    if len(cert_bytes) > _MAX_CERT_BYTES:
        raise SnsSignatureError("SNS signing certificate too large")
    return cert_bytes


def _store_signing_certificate(cert_url, cert_bytes):
    key = _signing_certificate_cache_key(cert_url)
    while len(_CERT_CACHE) >= _CERT_CACHE_MAX_ENTRIES:
        _CERT_CACHE.pop(next(iter(_CERT_CACHE)))
    _CERT_CACHE[key] = (monotonic() + _cert_cache_ttl_seconds(), cert_bytes)


def _signing_certificate_cache_key(cert_url):
    """Normalize the cache key so query-string variants share one entry."""
    parsed = urlparse(cert_url)
    return f"{parsed.scheme}://{(parsed.hostname or '').lower()}{parsed.path}"


def _is_allowed_sns_cert_url(cert_url):
    return _is_allowed_sns_url(cert_url, require_pem=True)


def _is_allowed_sns_subscription_url(subscribe_url):
    return _is_allowed_sns_url(subscribe_url, require_pem=False)


def _is_allowed_sns_url(url, *, require_pem):
    """Pin an SNS URL to real SNS hosts.

    Parses hostname (not netloc: netloc admits `sns.x@evil-host` userinfo
    tricks) and rejects ports, userinfo and (for certs) non-.pem paths.
    """
    try:
        parsed = urlparse(url)
        username, password, port = parsed.username, parsed.password, parsed.port
    except (TypeError, ValueError):
        return False
    if parsed.scheme != "https":
        return False
    if username or password:
        return False
    if port not in (None, 443):
        return False
    if not _is_allowed_sns_cert_host(parsed.hostname or ""):
        return False
    if require_pem and not parsed.path.endswith(".pem"):
        return False
    return True


def validate_topic_arn(payload):
    allowed = getattr(settings, "SES_WEBHOOKS_ALLOWED_TOPIC_ARNS", ())
    if not allowed:
        # Backwards compatible: deployments without an allowlist accept any
        # topic once the SNS signature verifies. Production should set
        # SES_WEBHOOKS_ALLOWED_TOPIC_ARNS; rogue topics still cannot
        # subscribe while SES_WEBHOOKS_ALLOW_SUBSCRIPTION_CONFIRMATION is off.
        global _WARNED_EMPTY_TOPIC_ALLOWLIST
        if not _WARNED_EMPTY_TOPIC_ALLOWLIST:
            _WARNED_EMPTY_TOPIC_ALLOWLIST = True
            logger.warning(
                "SES_WEBHOOKS_ALLOWED_TOPIC_ARNS is empty: any SNS topic with a valid "
                "signature is accepted. Set the allowlist in production."
            )
        return True
    topic_arn = payload.get("TopicArn", "")
    if not isinstance(topic_arn, str) or topic_arn not in tuple(allowed):
        raise SnsSignatureError("unknown SNS topic")
    return True


def canonical_sns_message(payload):
    fields_by_type = {
        SNS_NOTIFICATION: ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"],
        SNS_SUBSCRIPTION_CONFIRMATION: [
            "Message",
            "MessageId",
            "SubscribeURL",
            "Timestamp",
            "Token",
            "TopicArn",
            "Type",
        ],
    }
    parts = []
    for field in fields_by_type.get(payload.get("Type"), []):
        if field in payload:
            parts.append(f"{field}\n{payload[field]}\n")
    return "".join(parts)


def confirm_subscription(payload):
    if not getattr(settings, "SES_WEBHOOKS_ALLOW_SUBSCRIPTION_CONFIRMATION", False):
        return False
    if not getattr(settings, "SES_WEBHOOKS_ALLOWED_TOPIC_ARNS", ()):
        # Fail closed: confirming without an allowlist would subscribe any
        # AWS account's topic. Set SES_WEBHOOKS_ALLOWED_TOPIC_ARNS first.
        raise SnsSignatureError("subscription confirmation requires a topic allowlist")
    subscribe_url = payload.get("SubscribeURL", "")
    if not subscribe_url:
        raise SesWebhookError("missing SubscribeURL")
    # The confirmation GET leaves our network: pin it to SNS over HTTPS and
    # to the topic being confirmed, so a signed-but-hostile payload cannot
    # turn the endpoint into an SSRF probe.
    if not isinstance(subscribe_url, str) or not _is_allowed_sns_subscription_url(subscribe_url):
        raise SnsSignatureError("invalid subscription confirmation URL")
    try:
        query_topic = parse_qs(urlparse(subscribe_url).query).get("TopicArn", [""])[0]
    except (TypeError, ValueError):
        query_topic = ""
    payload_topic = payload.get("TopicArn", "")
    if not query_topic or query_topic != payload_topic:
        raise SnsSignatureError("subscription topic mismatch")
    try:
        with urlopen(subscribe_url, timeout=5):
            return True
    except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as exc:
        raise SnsSignatureError("subscription confirmation failed") from exc


def normalize_ses_notification(sns_payload, *, received_at=None):
    try:
        ses_payload = json.loads(sns_payload["Message"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SesWebhookError("invalid SES notification message") from exc
    if not isinstance(ses_payload, dict):
        raise SesWebhookError("SES notification message must be an object")

    event_name = ses_payload.get("eventType") or ses_payload.get("notificationType")
    notification_type = SES_EVENT_TYPES.get(event_name, "unsupported")

    mail = ses_payload.get("mail") or {}
    ses_message_id = mail.get("messageId") or ses_payload.get("mailMessageId") or ""
    metadata = build_ses_metadata(ses_payload, sns_payload)
    payload = {
        "contract": SES_WEBHOOKS_CONTRACT,
        "version": CONTRACT_VERSION,
        "provider": "ses",
        "provider_event_id": sns_payload["MessageId"],
        "notification_type": notification_type,
        "received_at": (received_at or timezone.now()).isoformat(),
        "metadata": metadata,
    }
    if ses_message_id:
        payload["ses_message_id"] = ses_message_id
        payload["mail_message_id"] = ses_message_id
    return validate_ses_webhook_message(payload)


def normalize_ses_webhook_worker_payload(payload, *, received_at=None, sqs_attributes=None):
    if not isinstance(payload, dict):
        raise SesWebhookError("SES webhook worker payload must be a JSON object")

    if payload.get("contract") == SES_WEBHOOKS_CONTRACT:
        normalized = validate_ses_webhook_message(payload)
    elif payload.get("Type") == SNS_NOTIFICATION:
        normalized = normalize_ses_notification(payload, received_at=received_at)
    elif payload.get("Type") == SNS_SUBSCRIPTION_CONFIRMATION:
        normalized = validate_ses_webhook_message(
            {
                "contract": SES_WEBHOOKS_CONTRACT,
                "version": CONTRACT_VERSION,
                "provider": "ses",
                "provider_event_id": payload.get("MessageId", ""),
                "notification_type": "unsupported",
                "received_at": (received_at or timezone.now()).isoformat(),
                "metadata": {
                    "sns_type": SNS_SUBSCRIPTION_CONFIRMATION,
                    "sns_message_id": payload.get("MessageId"),
                    "sns_topic_arn": payload.get("TopicArn"),
                },
            }
        )
    else:
        raise SesWebhookError("unsupported SES webhook worker payload")

    metadata = dict(normalized.get("metadata") or {})
    for attribute, key in (
        ("SentTimestamp", "sqs_sent_timestamp"),
        ("ApproximateFirstReceiveTimestamp", "sqs_approximate_first_receive_timestamp"),
    ):
        value = (sqs_attributes or {}).get(attribute)
        if value not in (None, ""):
            metadata[key] = value
    return normalized | {"metadata": metadata}


def _destination_hash(mail):
    destinations = mail.get("destination") or []
    if not destinations or not isinstance(destinations[0], str):
        return None
    return hashlib.sha256(destinations[0].strip().lower().encode("utf-8")).hexdigest()


def build_ses_metadata(ses_payload, sns_payload):
    event_name = ses_payload.get("eventType") or ses_payload.get("notificationType")
    notification_type = SES_EVENT_TYPES.get(event_name, "unsupported")
    detail = (
        ses_payload.get("deliveryDelay")
        or ses_payload.get(notification_type)
        or (ses_payload.get(event_name) if event_name else None)
        or {}
    )
    mail = ses_payload.get("mail") or {}
    metadata = {
        "sns_message_id": sns_payload.get("MessageId"),
        "sns_topic_arn": sns_payload.get("TopicArn"),
        "sns_timestamp": sns_payload.get("Timestamp"),
        # F3: per-type SES event time (delivery/bounce/complaint/open/click/
        # delivery_delay carry their own `timestamp`). event_timestamp() reads
        # this first so delivered/opened/clicked_at are stamped with the event
        # time, not the send (mail) time. Types without one (send, reject)
        # keep the mail_timestamp fallback.
        "event_timestamp": detail.get("timestamp") if isinstance(detail, dict) else None,
        "mail_timestamp": mail.get("timestamp"),
        "mail_source": mail.get("source"),
        # Hash of the first destination, so an UNKNOWN campaign recipient (send outcome
        # lost) can be matched to its SES events without storing the address here.
        "mail_destination_hash": _destination_hash(mail),
    }
    if notification_type == "bounce":
        metadata |= {
            "bounce_type": detail.get("bounceType"),
            "bounce_sub_type": detail.get("bounceSubType"),
        }
    elif notification_type == "complaint":
        metadata["complaint_feedback_type"] = detail.get("complaintFeedbackType")
    elif notification_type == "reject":
        metadata["reason"] = detail.get("reason")
    elif notification_type == "click":
        metadata["url"] = detail.get("link")
    elif notification_type == "delivery_delay":
        metadata["event_type"] = "DeliveryDelay"
        if detail.get("delayType"):
            metadata["delay_type"] = detail["delayType"]
        if detail.get("expirationTime"):
            metadata["expiration_time"] = detail["expirationTime"]
        delayed_recipients = detail.get("delayedRecipients") or []
        if isinstance(delayed_recipients, list) and delayed_recipients:
            diag = delayed_recipients[0].get("diagnosticCode", "")
            if diag:
                metadata["diagnostic_code"] = sanitize_for_logging(diag)
            status = delayed_recipients[0].get("status", "")
            if status:
                metadata["delayed_status"] = status
    elif notification_type == "unsupported":
        if event_name:
            metadata["event_type"] = str(event_name)
    return {key: value for key, value in metadata.items() if value not in (None, "")}


def record_unsupported_ses_event(payload):
    provider_event_id = payload["provider_event_id"]
    notification_type = payload["notification_type"]

    existing = OperatorAudit.objects.filter(
        action__endswith="unsupported",
        metadata__provider_event_id=provider_event_id,
    ).first()
    if existing:
        return existing

    with transaction.atomic():
        if connection.vendor == "postgresql":
            # An absent audit row cannot be row-locked. Serialize retries by
            # event ID, including events without a correlated message. The
            # transaction lock releases automatically on commit or rollback.
            lock_id = int.from_bytes(
                hashlib.sha256(f"ses-unsupported:{provider_event_id}".encode()).digest()[:8],
                signed=True,
            )
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])
        existing = OperatorAudit.objects.select_for_update().filter(
            action__endswith="unsupported",
            metadata__provider_event_id=provider_event_id,
        ).first()
        if existing:
            return existing

        source = correlate_ses_message(payload.get("ses_message_id", ""))
        target_type = "ses_notification"
        target_id = 0
        if source is not None:
            target_type = source.__class__.__name__.lower()
            target_id = source.id

        action = f"ses.{notification_type}.unsupported" if notification_type != "unsupported" else "ses.event.unsupported"

        metadata = safe_metadata(
            {
                "provider": payload.get("provider", "ses"),
                "provider_event_id": provider_event_id,
                "ses_message_id": payload.get("ses_message_id", ""),
                "notification_type": notification_type,
                **(payload.get("metadata") or {}),
            }
        )

        return OperatorAudit.objects.create(
            actor=None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata=metadata,
        )


def process_ses_webhook(payload):
    validate_ses_webhook_message(payload)
    provider_event_id = payload["provider_event_id"]
    notification_type = payload["notification_type"]

    if notification_type in {"delivery_delay", "unsupported"}:
        return record_unsupported_ses_event(payload)

    if EmailEvent.objects.filter(provider_event_id=provider_event_id).exists():
        return None

    try:
        with transaction.atomic():
            event, refresh_campaign_id = _append_and_apply_ses_event(payload, provider_event_id, notification_type)
    except IntegrityError:
        # No locking existence check inside the transaction: it would lock
        # nothing (the row does not exist yet) and costs a round trip per
        # event. Consumers racing on one provider_event_id (a redelivery
        # picked up by a second process) are settled by the unique index: the
        # loser fails its INSERT before applying any update and rolls back.
        if EmailEvent.objects.filter(provider_event_id=provider_event_id).exists():
            logger.info("ses_event_duplicate provider_event_id=%s (concurrent delivery)", provider_event_id)
            return None
        raise
    if event is None:
        return None

    if refresh_campaign_id is not None:
        try:
            refresh_campaign_provider_counts_if_due(refresh_campaign_id)
        except Exception:  # noqa: BLE001 - the event is already committed; the next event or drain refreshes
            logger.exception("Refreshing provider counts for campaign %s failed", refresh_campaign_id)
    return event


def _append_and_apply_ses_event(payload, provider_event_id, notification_type):
    """Body of the per-event transaction: returns ``(event, campaign_id_to_refresh)``."""
    source = correlate_ses_message(payload.get("ses_message_id", ""))
    if notification_type == "click" and isinstance(source, CampaignRecipient):
        if is_relay_tracker_url((payload.get("metadata") or {}).get("url", "")):
            # Campaign links are double-wrapped (SES click tracking -> Relay
            # /t/c/ -> target). Relay's redirector already recorded this
            # human click, so the SES event on the wrapper would count twice.
            logger.info("Ignoring SES click on Relay tracker URL for recipient %s", source.pk)
            return None, None
    event = append_provider_event(payload, source)
    if event is None:
        return None, None
    if source is None:
        logger.warning(
            "ses_event_uncorrelated provider_event_id=%s type=%s ses_message_id=%s "
            "(stored; reconciled once the message id is committed)",
            provider_event_id, notification_type, payload.get("ses_message_id", ""),
        )
    if source is not None:
        apply_correlated_updates(payload, source, event)
    refresh_campaign_id = source.campaign_id if isinstance(source, CampaignRecipient) else None
    return event, refresh_campaign_id


def correlate_ses_message(ses_message_id):
    if not ses_message_id:
        return None

    # Resolve uniqueness without locking, then lock only the child row. The
    # campaign row is deliberately NOT locked: senders hold it FOR SHARE per
    # recipient across the SES call, and a FOR UPDATE here per event made the
    # two starve each other (2026-11-09, campaign 7: ~0.3 events/s and
    # ~13 msg/s). Campaign counters are refreshed after commit instead
    # (``refresh_campaign_provider_counts_if_due``), so no webhook transaction
    # holds a recipient lock while waiting on the campaign: no deadlock cycle.
    candidates = list(CampaignRecipient.objects.filter(ses_message_id=ses_message_id)
                      .values("pk", "campaign_id")[:2])
    transactional_messages = (
        TransactionalMessage.objects.select_for_update(of=("self",))
        .select_related("client", "contact")
        .filter(ses_message_id=ses_message_id)
    )
    if len(candidates) == 1 and not transactional_messages.exists():
        candidate = candidates[0]
        return (CampaignRecipient.objects.select_for_update(of=("self",))
                .select_related("campaign", "campaign__client", "campaign__audience", "contact")
                .filter(pk=candidate["pk"], ses_message_id=ses_message_id).first())
    if transactional_messages.count() == 1 and not candidates:
        return transactional_messages.first()
    return None


def append_provider_event(payload, source):
    notification_type = payload["notification_type"]
    metadata = webhook_event_metadata(payload)
    url = metadata.get("url", "")
    event_type = EVENT_TYPE_BY_NOTIFICATION[notification_type]
    kwargs = {
        "event_type": event_type,
        "provider_event_id": payload["provider_event_id"],
        "url": url if notification_type == "click" else "",
        "metadata": metadata,
    }
    if isinstance(source, CampaignRecipient):
        kwargs |= {
            "campaign": source.campaign,
            "campaign_recipient": source,
            "contact": source.contact,
            "client": source.campaign.client,
            "audience": source.campaign.audience,
        }
    elif isinstance(source, TransactionalMessage):
        kwargs |= {
            "transactional_message": source,
            "contact": source.contact,
            "client": source.client,
        }
    return EmailEvent.objects.create(**kwargs)


def apply_correlated_updates(payload, source, event):
    notification_type = payload["notification_type"]
    occurred_at = event_timestamp(payload)
    metadata = payload.get("metadata") or {}
    if isinstance(source, CampaignRecipient):
        apply_campaign_recipient_update(source, notification_type, occurred_at, metadata)
    elif isinstance(source, TransactionalMessage):
        apply_transactional_message_update(source, notification_type, occurred_at, metadata)

    if notification_type in {"delivery", "open", "click", "complaint"} or (
        notification_type == "bounce" and is_hard_bounce(metadata)
    ):
        emit_cmp_contact_event(event, in_transaction=True)
    # Every correlated transactional transition is client-visible on the
    # client callback channel, including soft bounces: reason_code carries
    # the hard/soft distinction.
    emit_client_callback(event)


def apply_campaign_recipient_update(recipient, notification_type, occurred_at, metadata):
    update_fields = ["updated_at"]
    if notification_type == "delivery" and recipient.delivered_at is None:
        recipient.delivered_at = occurred_at
        update_fields.append("delivered_at")
    elif notification_type == "bounce":
        if is_hard_bounce(metadata):
            recipient.status = CampaignRecipientStatus.BOUNCED
            update_fields.append("status")
            mark_hard_bounced(recipient.contact, occurred_at)
        else:
            recipient.last_error = "soft_bounce"
            update_fields.append("last_error")
    elif notification_type == "complaint":
        recipient.status = CampaignRecipientStatus.COMPLAINED
        update_fields.append("status")
        mark_complained(recipient.contact, occurred_at)
    elif notification_type == "open":
        recipient.open_count += 1
        update_fields.append("open_count")
        if recipient.first_opened_at is None:
            recipient.first_opened_at = occurred_at
            update_fields.append("first_opened_at")
    elif notification_type == "click":
        recipient.click_count += 1
        update_fields.append("click_count")
        if recipient.first_clicked_at is None:
            recipient.first_clicked_at = occurred_at
            update_fields.append("first_clicked_at")
    elif notification_type == "reject":
        recipient.status = CampaignRecipientStatus.FAILED
        recipient.last_error = metadata.get("reason", "ses_reject")[:2000]
        update_fields += ["status", "last_error"]

    recipient.save(update_fields=sorted(set(update_fields)))


def apply_transactional_message_update(message, notification_type, occurred_at, metadata):
    update_fields = ["updated_at"]
    if notification_type == "delivery" and message.delivered_at is None:
        message.delivered_at = occurred_at
        update_fields.append("delivered_at")
    elif notification_type == "bounce":
        # SES publishes Bounce only after delivery attempts have ended, even
        # for a transient failure. Contact suppression still requires Permanent.
        message.status = TransactionalMessageStatus.BOUNCED
        update_fields.append("status")
        if is_hard_bounce(metadata):
            mark_hard_bounced(message.contact, occurred_at)
        else:
            message.last_error = "soft_bounce"
            update_fields.append("last_error")
    elif notification_type == "complaint":
        message.status = TransactionalMessageStatus.COMPLAINED
        update_fields.append("status")
        mark_complained(message.contact, occurred_at)
    elif notification_type == "open":
        message.open_count += 1
        update_fields.append("open_count")
        if message.first_opened_at is None:
            message.first_opened_at = occurred_at
            update_fields.append("first_opened_at")
    elif notification_type == "click":
        message.click_count += 1
        update_fields.append("click_count")
        if message.first_clicked_at is None:
            message.first_clicked_at = occurred_at
            update_fields.append("first_clicked_at")
    elif notification_type == "reject":
        message.status = TransactionalMessageStatus.FAILED
        message.last_error = metadata.get("reason", "ses_reject")[:2000]
        update_fields += ["status", "last_error"]

    message.save(update_fields=sorted(set(update_fields)))


RECONCILE_LOOKBACK_DAYS = 7
RECONCILE_MAX_EVENTS = 5000


def reconcile_uncorrelated_ses_events(campaign_id=None):
    """Correlate stored-uncorrelated SES events whose message id is now committed.

    A webhook can reach us before the sender commits ses_message_id; it is
    stored uncorrelated (deduped by provider_event_id). Each event is
    correlated under its own row lock and only if it is still uncorrelated, so
    its status/counter effects are applied exactly once even with concurrent
    reconcilers. Returns counts; everything is logged.
    """
    events = EmailEvent.objects.filter(
        campaign_recipient__isnull=True,
        transactional_message__isnull=True,
        created_at__gte=timezone.now() - timedelta(days=RECONCILE_LOOKBACK_DAYS),
    ).exclude(provider_event_id="").exclude(metadata__ses_message_id__isnull=True).exclude(metadata__ses_message_id="")
    if campaign_id is not None:
        message_ids = list(
            CampaignRecipient.objects.filter(campaign_id=campaign_id).exclude(ses_message_id="")
            .values_list("ses_message_id", flat=True)
        )
        events = events.filter(metadata__ses_message_id__in=message_ids)
    candidate_ids = list(events.order_by("id").values_list("pk", flat=True)[:RECONCILE_MAX_EVENTS])
    result = {"candidates": len(candidate_ids), "reconciled": 0, "still_uncorrelated": 0, "errors": 0}
    campaign_ids = set()
    for event_id in candidate_ids:
        try:
            with transaction.atomic():
                event = EmailEvent.objects.select_for_update().get(pk=event_id)
                if event.campaign_recipient_id or event.transactional_message_id:
                    continue  # another reconciler or the webhook path got it first
                source = correlate_ses_message(event.metadata.get("ses_message_id", ""))
                if source is None:
                    result["still_uncorrelated"] += 1
                    continue
                notification_type = event.metadata.get("notification_type", "")
                payload = {"notification_type": notification_type, "metadata": event.metadata,
                           "received_at": event.metadata.get("received_at", "")}
                if isinstance(source, CampaignRecipient):
                    event.campaign = source.campaign
                    event.campaign_recipient = source
                    event.contact = source.contact
                    event.client = source.campaign.client
                    event.audience = source.campaign.audience
                else:
                    event.transactional_message = source
                    event.contact = source.contact
                    event.client = source.client
                event.save()
                if (notification_type == "click" and isinstance(source, CampaignRecipient)
                        and is_relay_tracker_url(event.url)):
                    pass  # Relay's redirector already counted this click
                else:
                    apply_correlated_updates(payload, source, event)
                if isinstance(source, CampaignRecipient):
                    campaign_ids.add(source.campaign_id)
                result["reconciled"] += 1
        except Exception:  # noqa: BLE001 - logged loudly; other events continue
            result["errors"] += 1
            logger.exception("ses_reconcile_event_failed event_id=%s", event_id)
    for refresh_id in campaign_ids:
        try:
            refresh_campaign_provider_counts(refresh_id)
        except Exception:  # noqa: BLE001
            result["errors"] += 1
            logger.exception("ses_reconcile_refresh_failed campaign_id=%s", refresh_id)
    if result["candidates"]:  # quiet when there is nothing to reconcile
        log = logger.warning if result["errors"] or result["still_uncorrelated"] else logger.info
        log("ses_reconcile campaign_id=%s %s", campaign_id, result)
    return result


PROVIDER_COUNTS_REFRESH_INTERVAL_SECONDS = 5


def refresh_campaign_provider_counts_if_due(campaign_id):
    """Refresh counters at most once per interval per process, plus once the queue drains.

    Recomputing per event was O(recipients) under the campaign lock. Skipped
    events are not lost: the counters are a pure function of recipient rows,
    and the event that finds no provider-events task READY always refreshes,
    so the final totals are exact once the backlog is processed.
    """
    due = cache.add(
        f"campaign-provider-counts:{campaign_id}", 1, timeout=PROVIDER_COUNTS_REFRESH_INTERVAL_SECONDS
    )
    if not due and DBTaskResult.objects.filter(
        task_path="mailing.tasks.process_ses_webhook_event", status="READY"
    ).exists():
        return False
    refresh_campaign_provider_counts(campaign_id)
    return True


def refresh_campaign_provider_counts(campaign_id):
    # One pass over the campaign's recipients, written with a single UPDATE so
    # the campaign row is locked only for that statement.
    totals = CampaignRecipient.objects.filter(campaign_id=campaign_id).aggregate(
        delivered=Count("pk", filter=Q(delivered_at__isnull=False)),
        bounced=Count("pk", filter=Q(status=CampaignRecipientStatus.BOUNCED)),
        complained=Count("pk", filter=Q(status=CampaignRecipientStatus.COMPLAINED)),
        unique_opens=Count("pk", filter=Q(first_opened_at__isnull=False)),
        unique_clicks=Count("pk", filter=Q(first_clicked_at__isnull=False)),
        opens=Sum("open_count"),
        clicks=Sum("click_count"),
    )
    Campaign.objects.filter(pk=campaign_id).update(
        delivered_count=totals["delivered"],
        bounce_count=totals["bounced"],
        complaint_count=totals["complained"],
        unique_open_count=totals["unique_opens"],
        unique_click_count=totals["unique_clicks"],
        open_count=totals["opens"] or 0,
        click_count=totals["clicks"] or 0,
        updated_at=timezone.now(),
    )


def mark_hard_bounced(contact, occurred_at):
    Contact.objects.filter(pk=contact.pk, hard_bounced_at__isnull=True).update(
        hard_bounced_at=occurred_at,
        updated_at=timezone.now(),
    )


def mark_complained(contact, occurred_at):
    Contact.objects.filter(pk=contact.pk, complained_at__isnull=True).update(
        complained_at=occurred_at,
        updated_at=timezone.now(),
    )


def is_relay_tracker_url(url):
    """True when url is one of Relay's own /t/c/ click-redirect links."""
    if not url:
        return False
    parsed = urlparse(url)
    base = urlparse(settings.PUBLIC_BASE_URL)
    return (
        parsed.scheme in {"http", "https"}
        and (parsed.hostname or "").lower() == (base.hostname or "").lower()
        and parsed.path.startswith("/t/c/")
    )


def event_timestamp(payload):
    metadata = payload.get("metadata") or {}
    for key in ("event_timestamp", "mail_timestamp"):
        value = metadata.get(key)
        parsed = parse_datetime(value) if isinstance(value, str) else None
        if parsed is not None:
            return parsed if parsed.tzinfo else timezone.make_aware(parsed)
    parsed = parse_datetime(payload.get("received_at", ""))
    if parsed is not None:
        return parsed if parsed.tzinfo else timezone.make_aware(parsed)
    return timezone.now()


def webhook_event_metadata(payload):
    metadata = dict(payload.get("metadata") or {})
    metadata.update(
        {
            "provider": payload["provider"],
            "provider_event_id": payload["provider_event_id"],
            "notification_type": payload["notification_type"],
            "ses_message_id": payload.get("ses_message_id", ""),
            "received_at": payload["received_at"],
        }
    )
    return metadata


def _is_allowed_sns_cert_host(host):
    """Match real SNS cert hosts only: sns.<region>.amazonaws.com(.cn).

    Single region label on purpose: attacker-controlled subdomains such as
    sns.evil.<account>.s3.<region>.amazonaws.com must not match.
    """
    if not isinstance(host, str):
        return False
    return bool(_SNS_CERT_HOST_RE.fullmatch(host.lower()))
