"""Transport AISHL's signed SNS envelope without changing Relay email state.

AISHL remains the sole owner of SesEvent/user mutations. A successful HTTP
response is the durable acknowledgement; failures stay in SQS for retry/DLQ.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from mailing.aws import aws_client
from mailing.sqs import process_sqs_event

logger = logging.getLogger(__name__)
_secret_cache = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never send the shared-secret header to a redirect target.
        return None


def _shared_secret():
    global _secret_cache
    arn = settings.AISL_SES_WEBHOOK_SECRET_ARN
    now = time.monotonic()
    if _secret_cache is None or _secret_cache[0] != arn or now >= _secret_cache[1]:
        value = aws_client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"].strip()
        if not value:
            raise ImproperlyConfigured("AISHL SES webhook secret is empty.")
        _secret_cache = (arn, now + 300, value)
    return _secret_cache[2]


def validate_config():
    url = urlsplit(settings.AISL_SES_WEBHOOK_URL)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        raise ImproperlyConfigured("AISL_SES_WEBHOOK_URL must be an HTTPS URL without credentials or fragment.")
    if not settings.AISL_SES_WEBHOOK_SECRET_ARN or not settings.AISL_SES_TOPIC_ARNS:
        raise ImproperlyConfigured("AISHL SES ingress requires a secret ARN and allowed SNS topic ARNs.")


def forward_sns_notification(payload, record):
    validate_config()
    message_id = payload.get("MessageId") if isinstance(payload, dict) else None
    try:
        if (
            not isinstance(payload, dict)
            or payload.get("Type") != "Notification"
            or payload.get("TopicArn") not in settings.AISL_SES_TOPIC_ARNS
            or not isinstance(message_id, str)
            or not message_id
            or not isinstance(payload.get("Message"), str)
        ):
            raise ValueError("Unexpected AISHL SNS envelope")
        # Forward every signed field unchanged, including the original SNS ID
        # used for dedupe by both this transport and the temporary Lambda path.
        request = urllib.request.Request(
            settings.AISL_SES_WEBHOOK_URL,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-SES-Webhook-Secret": _shared_secret(),
            },
        )
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=15) as response:
            if response.status != 200:
                raise ValueError("Unexpected AISHL HTTP status")
            result = json.loads(response.read(4096))
        # "ignored" is a durable acknowledgement too: AISHL recorded the
        # audit row and decided the event is a no-op (malformed inner
        # Message or unknown kind get HTTP 200 precisely so the sender
        # stops retrying). Retrying those would only burn redrives.
        if not isinstance(result, dict) or result.get("status") not in {"ok", "duplicate", "ignored"}:
            raise ValueError("AISHL did not acknowledge the event")
    except Exception as exc:
        # No recipient addresses, bodies, response bodies, or secret values.
        logger.warning(
            "AISHL SES forward failed sns_message_id=%s error_type=%s http_status=%s",
            message_id if isinstance(message_id, str) else "invalid",
            type(exc).__name__,
            exc.code if isinstance(exc, urllib.error.HTTPError) else "none",
        )
        raise
    logger.info("AISHL SES forwarded sns_message_id=%s status=%s", message_id, result["status"])


def aisl_ses_ingress_handler(event, context=None):
    return process_sqs_event(event, forward_sns_notification)
