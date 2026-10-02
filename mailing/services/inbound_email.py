"""Filing accepted inbound mail into the mailbox.

This module used to parse each message, extract its body and every attachment,
publish an `email.received` event and keep nothing. That was right for a
Datamailer-era consumer holding its own system of record, and wrong for a
mailbox, where the reader is a person in this console. The service name and the
`process_inbound_s3_notification` entry point are unchanged so the ingress drain
and the legacy Lambda handler keep working; what the function returns is
different.

What changed, and why each part:

- **Messages are stored.** Headers, a snippet and the SES verdicts go to
  Postgres; bodies stay in object storage and are read on demand. Bodies stay in
  S3 because inbound mail is unbounded in size and retention, and the disk fill
  that disqualified inbound in production was a host-disk problem.

- **The blocklist is consulted before storage, not after.** Blocking a sender
  should stop future mail, not filter a mailbox that already has to be read, so a
  blocked message is discarded at ingest and recorded as a blocked row rather
  than a received one. That row is the audit trail for why a sender is blocked,
  and it deliberately carries no body.

- **Idempotency moved from DynamoDB to the message id.** The unique constraint
  on a non-empty message_id is the same guarantee the DynamoDB conditional write
  gave, inside the transaction that was already happening, with one fewer
  service to fail. INBOUND_EMAIL_IDEMPOTENCY_TABLE is no longer read.

- **Routes come from the InboundAddress table first.** Creating an address is a
  row rather than an edit to Terraform and an apply, and the environment
  variable is the fallback for the sandbox and for an estate that has created no
  addresses yet. The database wins so an address retired in the console also
  stops receiving, which a redeploy-free environment variable could not do.

- **The publish contract got smaller, and that is a real change.** When
  INBOUND_EMAIL_EVENTS_TOPIC_ARN is set the event is still published, but it now
  identifies the stored message and its raw MIME instead of carrying the body
  and every attachment. The stored message is the record, and a second full copy
  over SNS was the thing this path was built to stop doing. A consumer of `body`
  or `attachments` needs updating before the topic is enabled; see
  docs/worker-contracts.md.
"""

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import unquote_plus

from django.conf import settings
from django.db import IntegrityError, transaction

from mailing.aws import s3_client
from mailing.inbound_mime import parse_mime
from mailing.models import (
    BlockedSender,
    InboundAddress,
    InboundMessage,
    InboundMessageState,
)

logger = logging.getLogger(__name__)

CONTRACT = "inbound-email"
VERSION = 1
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
# Long enough to decide whether a pitch is a pitch, short enough that a list
# page does not carry megabytes of body text.
SNIPPET_CHARS = 500
BLOCKED_SNIPPET_CHARS = 120


def process_inbound_s3_notification(payload, *, s3=None, publisher=None):
    s3 = s3 or s3_client()
    results = []
    for record in payload.get("Records", []):
        if record.get("eventSource") != "aws:s3":
            raise ValueError("inbound worker accepts only S3 event records")
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        results.append(process_inbound_object(bucket, key, s3=s3, publisher=publisher))
    return results


def process_inbound_object(bucket, key, *, s3, publisher=None):
    raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    message = parse_mime(raw)

    if not message.message_id:
        raise ValueError("inbound MIME message is missing Message-ID")

    recipient = match_recipient(message.recipient_addresses)
    if recipient is None:
        logger.info("inbound message %s matched no Relay address; discarded", message.message_id)
        return {"discarded": "no_matching_address", "message_id": message.message_id}

    blocked = find_blocked_sender(message.sender_addresses)
    if blocked is not None:
        return file_blocked(message, blocked, bucket=bucket, key=key, size_bytes=len(raw))

    try:
        stored = file_message(message, bucket=bucket, key=key, size_bytes=len(raw), s3=s3, recipient=recipient)
    except IntegrityError:
        # The same message delivered twice. The old path signalled this with a
        # DynamoDB conditional write; the unique constraint says the same thing
        # and cannot half-succeed, because it is in the same transaction.
        logger.info("inbound message %s was already filed; treating as replay", message.message_id)
        return {"message_id": message.message_id, "replay": True}

    if stored.state == InboundMessageState.RECEIVED and settings.INBOUND_EMAIL_EVENTS_TOPIC_ARN:
        publish_event(publisher, stored, recipient)

    return {
        "message_id": stored.message_id,
        "inbound_message_id": stored.id,
        "state": stored.state,
        "replay": False,
    }


def match_recipient(recipients):
    """The Relay-owned address this message was delivered to.

    The database wins over the environment variable, and that order matters: an
    address created in the console has to receive mail without a redeploy, and
    an address retired in the console has to stop receiving it. The environment
    variable is what the sandbox runs on and what an estate with no addresses
    yet falls back to.
    """
    addresses = [address.lower() for address in recipients]

    for address in addresses:
        local_part, _, domain = address.partition("@")
        if not local_part or not domain:
            continue
        # Filtered on the two real columns rather than through the `address`
        # property, which is Python-side and not queryable.
        row = (
            InboundAddress.objects.filter(
                local_part__iexact=local_part,
                domain__iexact=domain,
                is_active=True,
            )
            .order_by("id")
            .first()
        )
        if row is not None:
            return row

    if settings.INBOUND_EMAIL_ROUTES:
        for address in addresses:
            if address in settings.INBOUND_EMAIL_ROUTES:
                local_part, _, domain = address.partition("@")
                return InboundAddress(
                    local_part=local_part or address,
                    domain=domain,
                    note="Matched from INBOUND_EMAIL_ROUTES; not a managed address.",
                )

    return None


def find_blocked_sender(sender_addresses):
    """The rule blocking this sender, if any.

    An exact address beats a domain rule, so blocking one correspondent at a
    shared sending domain does not silence everyone else on it.
    """
    addresses = [address.lower() for address in sender_addresses if address]
    domains = {address.partition("@")[2] for address in addresses if "@" in address}

    if addresses:
        rule = BlockedSender.objects.filter(
            scope=BlockedSender.Scope.ADDRESS, value__in=addresses
        ).first()
        if rule is not None:
            return rule

    if domains:
        return (
            BlockedSender.objects.filter(scope=BlockedSender.Scope.DOMAIN, value__in=sorted(domains))
            .order_by("id")
            .first()
        )

    return None


def file_message(message, *, bucket, key, size_bytes, s3, recipient):
    # Derived from the message id rather than the row, so a redelivery reuses the
    # same artifact keys and does not leave a second copy behind.
    artifact_id = hashlib.sha256(message.message_id.encode()).hexdigest()[:32]
    body_text_key, body_html_key = store_bodies(message, bucket=bucket, s3=s3, artifact_id=artifact_id)
    recipient_address = first_recipient(message.recipient_addresses, recipient)

    with transaction.atomic():
        row = InboundMessage.objects.create(
            message_id=message.message_id,
            state=InboundMessageState.RECEIVED,
            subject=message.subject[:998],
            snippet=snippet(message.body_text or message.text.preview),
            from_header=message.from_[:998],
            sender_address=(message.sender_addresses[0] if message.sender_addresses else "")[:320],
            sender_domain=sender_domain(message.sender_addresses),
            to_header=message.to[:998],
            cc_header=message.cc[:998],
            recipient=recipient.local_part[:320],
            recipient_domain=recipient.domain[:255],
            recipient_address=recipient_address[:320],
            inbound_address=recipient if recipient.pk else None,
            sent_at=parse_date(message.date),
            size_bytes=size_bytes,
            attachment_count=len(message.attachments),
            raw_bucket=bucket,
            raw_key=key,
            body_text_key=body_text_key,
            body_html_key=body_html_key,
            spam_verdict=verdict(message, "x-ses-spam-verdict"),
            virus_verdict=verdict(message, "x-ses-virus-verdict"),
            spf_verdict=verdict(message, "x-ses-spf-verdict"),
            dkim_verdict=verdict(message, "x-ses-dkim-verdict"),
            dmarc_verdict=verdict(message, "x-ses-dmarc-verdict"),
        )
    return row


def file_blocked(message, rule, *, bucket, key, size_bytes):
    """Record a discarded message so the block is visible and auditable.

    Deliberately does not store the body. The operator already decided this
    sender is not worth reading, and a blocklist that accumulates every pitch
    ever received is a spam archive rather than a filter.
    """
    with transaction.atomic():
        row = InboundMessage.objects.create(
            message_id=message.message_id,
            state=InboundMessageState.BLOCKED,
            subject=message.subject[:998],
            snippet=snippet(message.text.preview, limit=BLOCKED_SNIPPET_CHARS),
            from_header=message.from_[:998],
            sender_address=(message.sender_addresses[0] if message.sender_addresses else "")[:320],
            sender_domain=sender_domain(message.sender_addresses),
            to_header=message.to[:998],
            cc_header=message.cc[:998],
            recipient_address=(message.recipient_addresses[0] if message.recipient_addresses else "")[:320],
            size_bytes=size_bytes,
            raw_bucket=bucket,
            raw_key=key,
            blocked_rule=rule,
            spam_verdict=verdict(message, "x-ses-spam-verdict"),
        )
    return {"message_id": row.message_id, "inbound_message_id": row.id, "state": row.state, "replay": False}


def store_bodies(message, *, bucket, s3, artifact_id):
    """Keep the body parts in S3 and return their keys.

    The raw MIME is already in the bucket, so these are a convenience copy that
    spares the console from re-parsing a whole message to show one part. Both are
    written with SSE and neither is written when the part is empty.

    Keyed by artifact_id rather than by row id, because the row does not exist
    yet. That also makes a redelivery overwrite its own copies instead of
    accumulating a second set.
    """
    keys = []
    for value, filename, content_type in (
        (message.body_text, "body.txt", "text/plain"),
        (message.body_html, "body.html", "text/html"),
    ):
        if not value:
            keys.append("")
            continue
        object_key = f"{settings.INBOUND_EMAIL_ARTIFACT_PREFIX.rstrip('/')}/{artifact_id}/{filename}"
        s3.put_object(
            Bucket=bucket,
            Key=object_key,
            Body=value.encode(),
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )
        keys.append(object_key)
    return keys[0], keys[1]


def load_body_text(row, *, s3=None):
    """The stored plain-text body, or "" when there is none.

    HTML is not rendered: an inbound HTML body is untrusted third-party markup,
    and the console is an authenticated page that would be a poor place to
    execute it.
    """
    if not row.body_text_key:
        return ""
    s3 = s3 or s3_client()
    try:
        return s3.get_object(Bucket=row.raw_bucket, Key=row.body_text_key)["Body"].read().decode(
            "utf-8", errors="replace"
        )
    except Exception:
        logger.exception("could not read stored body for inbound message %s", row.pk)
        return ""


def sender_domain(sender_addresses):
    for address in sender_addresses:
        _, _, domain = address.partition("@")
        if domain:
            return domain[:255]
    return ""


def first_recipient(recipient_addresses, recipient):
    for address in recipient_addresses:
        if address.lower() == recipient.address.lower():
            return address
    return recipient.address


def verdict(message, name):
    return str(message.selected_headers.get(name, "")).strip()[:64]


def parse_date(value):
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def snippet(value, *, limit=SNIPPET_CHARS):
    return " ".join((value or "").split())[:limit]


def publish_event(publisher, row, recipient):
    publisher.publish(
        TopicArn=settings.INBOUND_EMAIL_EVENTS_TOPIC_ARN,
        Message=json.dumps(
            {
                "contract": CONTRACT,
                "version": VERSION,
                "event_type": "email.received",
                "occurred_at": datetime.now(UTC).isoformat(),
                "route": recipient.address,
                "message_id": row.message_id,
                "sender": {"header": row.from_header, "addresses": [row.sender_address]},
                "recipients": {"addresses": [row.recipient_address]},
                "subject": row.subject,
                "inbound_message_id": row.id,
                "raw_mime": {"bucket": row.raw_bucket, "key": row.raw_key},
            },
            sort_keys=True,
        ),
        MessageAttributes={
            "contract": {"DataType": "String", "StringValue": CONTRACT},
            "route": {"DataType": "String", "StringValue": recipient.address},
        },
    )


def safe_filename(value):
    return SAFE_FILENAME_RE.sub("_", value).strip("._") or "attachment"
