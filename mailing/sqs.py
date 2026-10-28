import json
import logging
import re

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from mailing.aws import sqs_client

logger = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
SECRET_PATTERN = re.compile(
    r"(?i)\b(api_key|token|secret|password|signature|authorization|access_key|private_key)\b"
    r"['\"]?\s*[:=]\s*"
    r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(?:Bearer|Basic)\s+[^\s,'";]+|[^\s,'";]+)'''
)


def sanitize_for_logging(message: str) -> str:
    if not message:
        return ""
    text = str(message)
    text = EMAIL_PATTERN.sub("[redacted-email]", text)
    text = SECRET_PATTERN.sub(r"\1=[redacted]", text)
    return text


def partial_batch_response(failed_message_ids):
    return {
        "batchItemFailures": [
            {"itemIdentifier": message_id}
            for message_id in failed_message_ids
        ]
    }


def records_from_messages(messages, *, event_source_arn="arn:aws:sqs:us-east-1:000000000000:test"):
    return {
        "Records": [
            {
                "messageId": message["MessageId"],
                "receiptHandle": message["ReceiptHandle"],
                "body": message["Body"],
                "attributes": message.get("Attributes", {}),
                "messageAttributes": message.get("MessageAttributes", {}),
                "md5OfBody": message.get("MD5OfBody", ""),
                "eventSource": "aws:sqs",
                "eventSourceARN": event_source_arn,
                "awsRegion": "us-east-1",
            }
            for message in messages
        ]
    }


def json_body(record):
    return json.loads(record["body"])


def process_sqs_event(event, handler):
    failed_message_ids = []
    unidentified_failure = False
    records = event.get("Records", []) if isinstance(event, dict) else []
    for record in records:
        message_id = record.get("messageId") or record.get("MessageId") or ""
        try:
            handler(json_body(record), record)
        except Exception as exc:
            msg_identifier = message_id or "<unknown>"
            exc_class = exc.__class__.__name__
            sanitized_reason = sanitize_for_logging(str(exc))
            logger.error(
                "SQS message %s failed (%s): %s",
                msg_identifier,
                exc_class,
                sanitized_reason,
            )
            if message_id:
                failed_message_ids.append(message_id)
            else:
                unidentified_failure = True

    if unidentified_failure:
        # If any message failed without an identifiable messageId, mark ALL records as failed
        # to ensure no message is deleted that failed to persist.
        all_ids = [r.get("messageId") or r.get("MessageId") for r in records if isinstance(r, dict)]
        return partial_batch_response([m for m in all_ids if m])

    return partial_batch_response(failed_message_ids)


def send_sqs_json_message(*, queue_url, payload, client=None):
    if not queue_url:
        raise ImproperlyConfigured("SQS queue URL is required.")

    sqs = client or sqs_client()
    return sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(payload, sort_keys=True),
    )


def enqueue_transactional_email(payload, *, client=None):
    return send_sqs_json_message(
        queue_url=settings.SQS_TRANSACTIONAL_EMAIL_QUEUE_URL,
        payload=payload,
        client=client,
    )


def enqueue_campaign_email(payload, *, client=None):
    return send_sqs_json_message(
        queue_url=settings.SQS_CAMPAIGN_EMAIL_QUEUE_URL,
        payload=payload,
        client=client,
    )


def enqueue_ses_webhook(payload, *, client=None):
    return send_sqs_json_message(
        queue_url=settings.SQS_SES_WEBHOOKS_QUEUE_URL,
        payload=payload,
        client=client,
    )
