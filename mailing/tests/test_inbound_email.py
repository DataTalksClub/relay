"""Inbound mail is filed into the mailbox, and a blocklist stops it first.

The service this replaced parsed each message, published an `email.received`
event and kept nothing. These tests cover what replaced that: a message becomes
a row, a message from a blocked sender becomes a different row, and neither
happens twice. Every assertion here fails if the corresponding behaviour breaks,
which is the standard this repository holds its tests to.

The AWS surface is faked with keyword injection rather than stubbed, so the test
never needs a network or credentials. There is no DynamoDB any more: idempotency
is a unique constraint on the message id, so there is nothing to fake.
"""

import io
import json
from email.message import EmailMessage

import pytest
from django.test import override_settings

from mailing.inbound_mime import address_values, parse_mime
from mailing.models import BlockedSender, InboundAddress, InboundMessage, InboundMessageState
from mailing.services.inbound_email import process_inbound_s3_notification

pytestmark = pytest.mark.django_db


class FakeS3:
    def __init__(self, raw=None):
        self.raw = raw
        self.puts = []
        self.objects = {}

    def get_object(self, Bucket, Key):
        if Key in self.objects:
            return {"Body": io.BytesIO(self.objects[Key])}
        return {"Body": io.BytesIO(self.raw)}

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        self.puts.append({"Bucket": Bucket, "Key": Key, "Body": Body, **kwargs})
        self.objects[Key] = Body


class FakePublisher:
    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    def publish(self, **kwargs):
        if self.fail:
            raise RuntimeError("publish failed")
        self.messages.append(kwargs)


def raw_message(
    *,
    message_id="<mail-123@example.com>",
    sender="Billing <billing@example.com>",
    to="Invoice <invoice@mailer.test>, TODO <todo@mailer.test>",
    subject="Receipt for July",
    spam_verdict=None,
):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Cc"] = "Copy <copy@example.com>"
    message["Subject"] = subject
    message["Date"] = "Sun, 12 Jul 2026 09:30:00 +0200"
    if message_id is not None:
        message["Message-ID"] = message_id
    if spam_verdict is not None:
        message["X-SES-Spam-Verdict"] = spam_verdict
    message.set_content("Plain receipt body")
    message.add_alternative("<html><body><strong>HTML receipt body</strong></body></html>", subtype="html")
    message.add_attachment(b"pdf bytes", maintype="application", subtype="pdf", filename="receipt July.pdf")
    return message.as_bytes()


def s3_event(key="raw%2Fmessage-123", bucket="inbound-private"):
    return {
        "Records": [
            {
                "eventSource": "aws:s3",
                "s3": {"bucket": {"name": bucket}, "object": {"key": key}},
            }
        ]
    }


def deliver(**kwargs):
    """Run the worker over one S3 record and return that record's result.

    The worker returns a list, one entry per record, because a single
    notification can carry several. Every test here sets up exactly one.
    """
    results = process_inbound_s3_notification(s3_event(**kwargs.pop("event", {})), **kwargs)
    assert len(results) == 1
    return results[0]


def managed_address(local_part="invoice", domain="mailer.test"):
    return InboundAddress.objects.create(local_part=local_part, domain=domain)


def test_parser_extracts_alternatives_addresses_and_attachment():
    parsed = parse_mime(raw_message())

    assert parsed.sender_addresses == ["billing@example.com"]
    assert parsed.recipient_addresses == ["invoice@mailer.test", "todo@mailer.test", "copy@example.com"]
    assert parsed.body_text.strip() == "Plain receipt body"
    assert "HTML receipt body" in parsed.body_html
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].filename == "receipt July.pdf"
    assert parsed.attachments[0].payload == b"pdf bytes"


def test_address_values_ignores_display_names():
    assert address_values('"Doe, Jane" <jane@example.com>') == ["jane@example.com"]


@override_settings(INBOUND_EMAIL_ROUTES={}, INBOUND_EMAIL_ARTIFACT_PREFIX="processed/")
def test_message_is_filed_with_headers_bodies_and_verdicts():
    managed_address()
    s3 = FakeS3(raw_message(spam_verdict="Yes"))

    result = deliver(s3=s3)

    row = InboundMessage.objects.get()
    assert result["state"] == InboundMessageState.RECEIVED
    assert row.subject == "Receipt for July"
    assert row.sender_address == "billing@example.com"
    assert row.sender_domain == "example.com"
    assert row.recipient_address == "invoice@mailer.test"
    assert row.inbound_address.local_part == "invoice"
    assert row.attachment_count == 1
    assert row.size_bytes == len(s3.raw)
    assert row.is_spam is True
    assert row.snippet.startswith("Plain receipt body")
    # The claimed send date is parsed, not the receipt date.
    assert row.sent_at.year == 2026
    assert row.sent_at.month == 7
    # Bodies are stored out of band, and the raw MIME is only pointed at.
    assert row.raw_key == "raw/message-123"
    assert row.body_text_key.endswith("body.txt")
    assert row.body_html_key.endswith("body.html")
    assert row.raw_bucket == "inbound-private"
    stored_keys = {put["Key"] for put in s3.puts}
    assert row.body_text_key in stored_keys
    assert row.body_html_key in stored_keys
    assert all(put["ServerSideEncryption"] == "AES256" for put in s3.puts)


@override_settings(INBOUND_EMAIL_ROUTES={}, INBOUND_EMAIL_ARTIFACT_PREFIX="processed/")
def test_bodies_of_different_messages_do_not_overwrite_each_other():
    """The artifact key is per-message, not a single shared name.

    A shared key would make the second message's body replace the first's, and
    the failure would look like a rendering bug rather than a lost message.
    """
    address = managed_address()
    first = deliver(event={"key": "raw%2Fone"}, s3=FakeS3(raw_message(message_id="<one@example.com>")))
    second = deliver(event={"key": "raw%2Ftwo"}, s3=FakeS3(raw_message(message_id="<two@example.com>")))

    one = InboundMessage.objects.get(pk=first["inbound_message_id"])
    two = InboundMessage.objects.get(pk=second["inbound_message_id"])
    assert one.body_text_key != two.body_text_key
    assert one.inbound_address_id == two.inbound_address_id == address.pk


@override_settings(INBOUND_EMAIL_ROUTES={}, INBOUND_EMAIL_ARTIFACT_PREFIX="processed/")
def test_redelivery_of_the_same_message_is_a_replay_not_a_second_row():
    managed_address()
    s3 = FakeS3(raw_message())

    deliver(s3=s3)
    second = deliver(s3=s3)

    assert InboundMessage.objects.count() == 1
    assert second["replay"] is True


@override_settings(INBOUND_EMAIL_ROUTES={}, INBOUND_EMAIL_ARTIFACT_PREFIX="processed/")
def test_address_match_is_case_insensitive():
    managed_address(local_part="Invoice", domain="Mailer.Test")
    address = InboundAddress.objects.get()

    deliver(s3=FakeS3(raw_message()))

    row = InboundMessage.objects.get()
    assert row.inbound_address_id == address.pk
    assert row.recipient_address == "invoice@mailer.test"


@override_settings(INBOUND_EMAIL_ROUTES={}, INBOUND_EMAIL_ARTIFACT_PREFIX="processed/")
def test_message_for_an_unknown_address_is_discarded():
    s3 = FakeS3(raw_message())

    result = deliver(s3=s3)

    assert result["discarded"] == "no_matching_address"
    assert not InboundMessage.objects.exists()
    # Nothing is written either: an unroutable message is not worth a body copy.
    assert s3.puts == []


@override_settings(INBOUND_EMAIL_ROUTES={}, INBOUND_EMAIL_ARTIFACT_PREFIX="processed/")
def test_a_retired_address_stops_receiving():
    address = managed_address()
    address.is_active = False
    address.save(update_fields=["is_active"])

    result = deliver(s3=FakeS3(raw_message()))

    assert result["discarded"] == "no_matching_address"
    assert not InboundMessage.objects.exists()


@override_settings(
    INBOUND_EMAIL_ROUTES={"invoice@mailer.test": "invoice"},
    INBOUND_EMAIL_ARTIFACT_PREFIX="processed/",
)
def test_environment_routes_still_work_when_no_managed_address_exists():
    """The sandbox runs on INBOUND_EMAIL_ROUTES and must keep receiving.

    The database wins when it has an address, and the environment is the
    fallback, so an estate that has created no addresses is not silently dark.
    """
    result = deliver(s3=FakeS3(raw_message()))

    row = InboundMessage.objects.get()
    assert result["state"] == InboundMessageState.RECEIVED
    assert row.recipient_address == "invoice@mailer.test"
    # Not a managed address, so the row is not filed under one.
    assert row.inbound_address_id is None


@override_settings(
    INBOUND_EMAIL_ROUTES={"invoice@mailer.test": "invoice"},
    INBOUND_EMAIL_ARTIFACT_PREFIX="processed/",
)
def test_a_managed_address_takes_precedence_over_the_environment_route():
    managed_address()
    InboundMessage.objects.create(
        message_id="<older@example.com>",
        recipient_address="invoice@mailer.test",
        inbound_address=InboundAddress.objects.get(),
    )

    deliver(s3=FakeS3(raw_message()))

    assert InboundMessage.objects.get(message_id="<mail-123@example.com>").inbound_address_id is not None


@override_settings(INBOUND_EMAIL_ROUTES={}, INBOUND_EMAIL_ARTIFACT_PREFIX="processed/")
def test_blocked_sender_is_discarded_and_recorded_without_a_body():
    """Blocking stops storage, not just display.

    A blocklist that still files every message is a filter an operator has to
    re-apply by hand, which is the thing it was meant to replace.
    """
    managed_address()
    rule = BlockedSender.objects.create(scope=BlockedSender.Scope.ADDRESS, value="billing@example.com")
    s3 = FakeS3(raw_message())

    result = deliver(s3=s3)

    row = InboundMessage.objects.get()
    assert result["state"] == InboundMessageState.BLOCKED
    assert row.state == InboundMessageState.BLOCKED
    assert row.blocked_rule_id == rule.pk
    assert row.sender_address == "billing@example.com"
    assert row.body_text_key == ""
    assert row.raw_key == "raw/message-123"
    assert s3.puts == []


@override_settings(INBOUND_EMAIL_ROUTES={}, INBOUND_EMAIL_ARTIFACT_PREFIX="processed/")
def test_a_domain_block_catches_every_sender_on_it():
    managed_address()
    BlockedSender.objects.create(scope=BlockedSender.Scope.DOMAIN, value="example.com")

    result = deliver(s3=FakeS3(raw_message()))

    assert result["state"] == InboundMessageState.BLOCKED
    assert InboundMessage.objects.get().state == InboundMessageState.BLOCKED


@override_settings(INBOUND_EMAIL_ROUTES={}, INBOUND_EMAIL_ARTIFACT_PREFIX="processed/")
def test_an_address_block_wins_over_a_domain_block_for_that_sender():
    """One correspondent at a shared sending domain can be blocked alone.

    Both rules match other@example.com, and the narrower one is the one an
    operator means when they clicked the address and not the domain.
    """
    managed_address()
    BlockedSender.objects.create(scope=BlockedSender.Scope.DOMAIN, value="example.com")
    address_rule = BlockedSender.objects.create(scope=BlockedSender.Scope.ADDRESS, value="other@example.com")

    deliver(
        event={"key": "raw%2Fother"},
        s3=FakeS3(raw_message(message_id="<other@example.com>", sender="Other <other@example.com>")),
    )

    blocked_row = InboundMessage.objects.get(message_id="<other@example.com>")
    assert blocked_row.state == InboundMessageState.BLOCKED
    assert blocked_row.blocked_rule_id == address_rule.pk



@override_settings(INBOUND_EMAIL_ROUTES={}, INBOUND_EMAIL_ARTIFACT_PREFIX="processed/")
def test_unblocking_lets_the_sender_through_again():
    managed_address()
    rule = BlockedSender.objects.create(scope=BlockedSender.Scope.ADDRESS, value="billing@example.com")
    process_inbound_s3_notification(s3_event(), s3=FakeS3(raw_message()))
    rule.delete()

    deliver(event={"key": "raw%2Fsecond"}, s3=FakeS3(raw_message(message_id="<second@example.com>")))

    assert InboundMessage.objects.get(message_id="<second@example.com>").state == InboundMessageState.RECEIVED


@override_settings(
    INBOUND_EMAIL_ROUTES={},
    INBOUND_EMAIL_ARTIFACT_PREFIX="processed/",
    INBOUND_EMAIL_EVENTS_TOPIC_ARN="arn:aws:sns:eu-west-1:123:inbound-events",
)
def test_event_is_published_when_a_topic_is_configured():
    managed_address()
    publisher = FakePublisher()

    deliver(s3=FakeS3(raw_message()), publisher=publisher)

    assert len(publisher.messages) == 1
    body = json.loads(publisher.messages[0]["Message"])
    assert body["contract"] == "inbound-email"
    assert body["event_type"] == "email.received"
    assert body["route"] == "invoice@mailer.test"
    assert body["inbound_message_id"] == InboundMessage.objects.get().pk
    assert body["raw_mime"] == {"bucket": "inbound-private", "key": "raw/message-123"}


@override_settings(
    INBOUND_EMAIL_ROUTES={},
    INBOUND_EMAIL_ARTIFACT_PREFIX="processed/",
    INBOUND_EMAIL_EVENTS_TOPIC_ARN="arn:aws:sns:eu-west-1:123:inbound-events",
)
def test_a_blocked_message_is_not_announced():
    managed_address()
    BlockedSender.objects.create(scope=BlockedSender.Scope.ADDRESS, value="billing@example.com")
    publisher = FakePublisher()

    deliver(s3=FakeS3(raw_message()), publisher=publisher)

    assert publisher.messages == []


@override_settings(INBOUND_EMAIL_ROUTES={}, INBOUND_EMAIL_ARTIFACT_PREFIX="processed/")
def test_nothing_is_published_when_no_topic_is_configured():
    """No topic means no publish, not a failed task.

    A Relay that owns a mailbox does not need a second copy announced over SNS,
    and requiring one would make the mailbox depend on a consumer that may not
    exist.
    """
    managed_address()
    publisher = FakePublisher()

    deliver(s3=FakeS3(raw_message()), publisher=publisher)

    assert publisher.messages == []
    assert InboundMessage.objects.count() == 1


@override_settings(INBOUND_EMAIL_ROUTES={}, INBOUND_EMAIL_ARTIFACT_PREFIX="processed/")
def test_a_long_message_id_is_truncated_rather_than_failing_the_write():
    """The unique index is a btree; a 998-character id would exceed its limit.

    Truncating keeps the dedupe guarantee and moves the failure from an insert
    error to a shortened value.
    """
    managed_address()
    long_id = "<" + ("a" * 1200) + "@example.com>"

    deliver(s3=FakeS3(raw_message(message_id=long_id)))

    row = InboundMessage.objects.get()
    assert len(row.message_id) == 512


@override_settings(INBOUND_EMAIL_ROUTES={}, INBOUND_EMAIL_ARTIFACT_PREFIX="processed/")
@pytest.mark.parametrize(
    "raw,error",
    [(b"", "empty MIME"), (b"not a MIME message", "no headers")],
)
def test_garbage_is_rejected_before_anything_is_stored(raw, error):
    managed_address()

    with pytest.raises(ValueError, match=error):
        deliver(s3=FakeS3(raw))

    assert not InboundMessage.objects.exists()


@override_settings(INBOUND_EMAIL_ROUTES={}, INBOUND_EMAIL_ARTIFACT_PREFIX="processed/")
def test_a_message_without_a_message_id_is_rejected():
    managed_address()

    with pytest.raises(ValueError, match="missing Message-ID"):
        deliver(s3=FakeS3(raw_message(message_id=None)))

    assert not InboundMessage.objects.exists()


def test_non_s3_records_are_refused():
    with pytest.raises(ValueError, match="only S3 event records"):
        process_inbound_s3_notification(
            {"Records": [{"eventSource": "aws:sqs"}]}, s3=FakeS3(raw_message())
        )
