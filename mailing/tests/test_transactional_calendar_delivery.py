"""Contract tests for the optional calendar_alternative native delivery slice.

Covers the authorized Relay #37 slice: the authenticated single-recipient API
request carries the accepted calendar profile through the durable message
metadata, the queue payload, and the worker into the existing SES raw
transport (mocked provider). The outgoing raw bytes are parsed and compared
as decoded full content, never as builder internals or file extensions.
"""

import json
from email import message_from_bytes

import boto3
import pytest
from botocore.stub import Stubber
from django.test import override_settings
from django.urls import reverse

from mailing.calendar_mime import MAX_CALENDAR_CONTENT_LENGTH
from mailing.models import (
    Audience,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    Organization,
    RecipientList,
    RecipientListMember,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.queue_contracts import validate_transactional_email_message
from mailing.services.auth import create_client_api_key
from mailing.services.transactional import build_transactional_queue_payload
from mailing.sqs import records_from_messages
from mailing.workers import transactional_email_handler

pytestmark = pytest.mark.django_db(transaction=True)

API_KEY = "calendar-test-key"

CALENDAR_CONTENT = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Test//Calendar//EN\r\n"
    "METHOD:REQUEST\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:adoption-review@test\r\n"
    "SEQUENCE:0\r\n"
    "DTSTART:20261005T090000Z\r\n"
    "DTEND:20261005T100000Z\r\n"
    "SUMMARY:Adoption review\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def api_client_record(organization):
    client = Client.objects.create(
        organization=organization,
        name="DTC Courses",
        slug="dtc-courses",
        default_sender_id="newsletter",
        sender_emails=[{"id": "newsletter", "email": "newsletter@example.com"}],
    )
    create_client_api_key(client=client, name="Calendar test", raw_api_key=API_KEY)
    return client


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="DataTalksClub", slug="datatalks-club")


@pytest.fixture
def contact():
    return Contact.objects.create(email="person@example.com")


@pytest.fixture
def template(api_client_record):
    return EmailTemplate.objects.create(
        client=api_client_record,
        key="email-verification",
        name="Email verification",
        subject="Verify {{ product }}",
        html_body="<p>Verify at {{ verification_url }}</p>",
        text_body="Verify at {{ verification_url }}",
    )


@pytest.fixture
def transactional_message(api_client_record, contact, template):
    return TransactionalMessage.objects.create(
        client=api_client_record,
        contact=contact,
        email=contact.normalized_email,
        from_email_id="courses",
        from_email="courses@dtcdev.click",
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.QUEUED,
        idempotency_key="verify-123",
        subject="Persisted subject",
        html_body="<p>Persisted body</p>",
        text_body="Persisted body",
    )


def auth_headers(raw_key=API_KEY):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def post_transactional(django_client, payload, raw_key=API_KEY):
    return django_client.post(
        reverse("mailing:api_transactional_send"),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def post_recipient_list_transactional(django_client, list_key, payload, raw_key=API_KEY):
    return django_client.post(
        reverse("mailing:api_recipient_list_transactional_send", args=[list_key]),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def post_transient_recipient_list_transactional(django_client, payload, raw_key=API_KEY):
    return django_client.post(
        reverse("mailing:api_transient_recipient_list_transactional_send"),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def calendar_send_payload(**overrides):
    payload = {
        "email": "person@example.com",
        "template_key": "email-verification",
        "idempotency_key": "calendar-123",
        "context": {
            "product": "Datamailer",
            "verification_url": "https://example.com/verify/token",
        },
        "calendar_alternative": {"content": CALENDAR_CONTENT, "method": "REQUEST"},
    }
    payload |= overrides
    return payload


def collect_enqueued(monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    return enqueued


class FakeRawSesClient:
    def __init__(self):
        self.raw_params = None

    def send_raw_email(self, **params):
        self.raw_params = params
        return {"MessageId": "raw-ses-message-123"}


class RecordingSesClient:
    # Records which provider send methods ran, so tests can assert the send
    # path was (or was not) reached through the patched client factory.
    def __init__(self):
        self.calls = []

    def send_email(self, **params):
        self.calls.append("send_email")
        return {"MessageId": "ses-message-123"}

    def send_raw_email(self, **params):
        self.calls.append("send_raw_email")
        return {"MessageId": "raw-ses-message-123"}


def _message(message_id, payload):
    return {
        "MessageId": message_id,
        "ReceiptHandle": f"{message_id}-receipt",
        "Body": json.dumps(payload),
    }


def _event(message_id, payload):
    return records_from_messages([_message(message_id, payload)])


@override_settings(DEFAULT_FROM_EMAIL="sender@example.com", AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="")
def test_calendar_alternative_flows_from_api_to_raw_mime_bytes(client, api_client_record, template, monkeypatch):
    enqueued = collect_enqueued(monkeypatch)
    ses = FakeRawSesClient()
    monkeypatch.setattr("mailing.services.transactional_sender.ses_client_for_source", lambda source: ses)

    response = post_transactional(client, calendar_send_payload())

    assert response.status_code == 202
    message = TransactionalMessage.objects.get()
    event = EmailEvent.objects.get()
    assert response.json()["message"]["id"] == message.id
    assert response.json()["enqueued"] is True
    # The name default is applied by the MIME validator, so the persisted
    # profile is deterministic even when the request omits it.
    assert message.metadata["calendar_alternative"] == {
        "content": CALENDAR_CONTENT,
        "method": "REQUEST",
        "name": "event.ics",
    }
    assert event.event_type == EmailEventType.QUEUED
    assert len(enqueued) == 1
    assert validate_transactional_email_message(enqueued[0]) == enqueued[0]
    assert enqueued[0]["transactional_message_id"] == message.id
    assert enqueued[0]["metadata"]["calendar_alternative"] == message.metadata["calendar_alternative"]

    worker_response = transactional_email_handler(_event("calendar-1", enqueued[0]))

    assert worker_response == {"batchItemFailures": []}
    message.refresh_from_db()
    assert message.status == TransactionalMessageStatus.SENT
    assert message.ses_message_id == "raw-ses-message-123"

    parsed = message_from_bytes(ses.raw_params["RawMessage"]["Data"])
    assert parsed.get_content_type() == "multipart/alternative"
    parts = parsed.get_payload()
    assert [part.get_content_type() for part in parts] == ["text/plain", "text/html", "text/calendar"]
    calendar_part = parts[-1]
    assert calendar_part.get("Content-Disposition") is None
    assert calendar_part.get_content_disposition() is None
    assert calendar_part["Content-Transfer-Encoding"] == "base64"
    assert calendar_part.get_content_charset() == "utf-8"
    assert calendar_part.get_param("method") == "REQUEST"
    assert calendar_part.get_param("name") == "event.ics"
    assert calendar_part.get_payload(decode=True).decode("utf-8") == CALENDAR_CONTENT
    assert parsed["To"] == "person@example.com"


@pytest.mark.parametrize(
    ("calendar_alternative", "code"),
    [
        ("not-an-object", "must_be_object"),
        (["BEGIN:VCALENDAR"], "must_be_object"),
        (42, "must_be_object"),
        ({"method": "REQUEST"}, "invalid"),
        ({"content": CALENDAR_CONTENT}, "invalid"),
        ({"content": CALENDAR_CONTENT, "method": "REQUEST", "extra": True}, "invalid"),
        ({"content": 5, "method": "REQUEST"}, "invalid"),
        ({"content": "x" * (MAX_CALENDAR_CONTENT_LENGTH + 1), "method": "REQUEST"}, "too_large"),
        ({"content": CALENDAR_CONTENT, "method": "PUBLISH"}, "invalid"),
        (
            {"content": CALENDAR_CONTENT.replace("METHOD:REQUEST", "METHOD:CANCEL"), "method": "REQUEST"},
            "invalid",
        ),
        ({"content": "not a calendar", "method": "REQUEST"}, "invalid"),
        (
            {"content": CALENDAR_CONTENT + "BEGIN:VCALENDAR\nEND:VCALENDAR\n", "method": "REQUEST"},
            "invalid",
        ),
        ({"content": CALENDAR_CONTENT, "method": "REQUEST", "name": "../escape.ics"}, "invalid"),
        ({"content": CALENDAR_CONTENT, "method": "REQUEST", "name": "a/b.ics"}, "invalid"),
        ({"content": CALENDAR_CONTENT, "method": "REQUEST", "name": "invite\x01.ics"}, "invalid"),
    ],
    ids=[
        "string-body",
        "list-body",
        "number-body",
        "missing-content",
        "missing-method",
        "extra-key",
        "non-string-content",
        "oversized-content",
        "undeclared-method",
        "method-mismatch",
        "no-envelope",
        "double-envelope",
        "traversal-name",
        "slashed-name",
        "control-char-name",
    ],
)
def test_calendar_validation_rejects_bad_profiles_without_mutating(
    client,
    template,
    monkeypatch,
    calendar_alternative,
    code,
):
    enqueued = collect_enqueued(monkeypatch)

    response = post_transactional(client, calendar_send_payload(calendar_alternative=calendar_alternative))

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"calendar_alternative": code}
    assert Contact.objects.count() == 0
    assert TransactionalMessage.objects.count() == 0
    assert EmailEvent.objects.count() == 0
    assert enqueued == []


def test_calendar_alternative_conflicts_with_message_parts_without_persisting(
    client,
    template,
    monkeypatch,
):
    enqueued = collect_enqueued(monkeypatch)
    Contact.objects.create(email="person@example.com")

    response = post_transactional(
        client,
        calendar_send_payload(
            message_parts=[
                {
                    "content_type": "text/plain",
                    "content": "ordinary attachment",
                    "filename": "notes.txt",
                    "disposition": "attachment",
                }
            ],
        ),
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"calendar_alternative": "conflicts_with_message_parts"}
    assert Contact.objects.count() == 1
    assert TransactionalMessage.objects.count() == 0
    assert EmailEvent.objects.count() == 0
    assert enqueued == []


def test_calendar_alternative_rejects_protected_headers_but_allows_them_without_calendar(
    client,
    template,
    monkeypatch,
):
    enqueued = collect_enqueued(monkeypatch)

    with_calendar = post_transactional(
        client,
        calendar_send_payload(idempotency_key="calendar-protected", headers={"Mime-Version": "1.0"}),
    )
    plain_payload = calendar_send_payload(
        idempotency_key="ordinary-protected",
        headers={"Mime-Version": "1.0"},
    )
    del plain_payload["calendar_alternative"]
    without_calendar = post_transactional(client, plain_payload)

    assert with_calendar.status_code == 400
    assert with_calendar.json()["error"]["fields"] == {"calendar_alternative": "invalid"}
    assert without_calendar.status_code == 202
    message = TransactionalMessage.objects.get()
    assert message.metadata["headers"] == {"Mime-Version": "1.0"}
    assert len(enqueued) == 1


def test_calendar_dry_run_validates_and_writes_nothing(client, template, monkeypatch):
    enqueued = collect_enqueued(monkeypatch)

    response = post_transactional(client, calendar_send_payload(dry_run=True))

    assert response.status_code == 202
    body = response.json()
    assert body["rendered"]["subject"] == "Verify Datamailer"
    assert body["would_deliver"] is True
    assert body["enqueued"] is False
    assert body["message"]["id"] is None
    assert enqueued == []
    assert TransactionalMessage.objects.count() == 0
    assert EmailEvent.objects.count() == 0


def test_calendar_dry_run_rejects_invalid_profile_like_a_real_send(client, template, monkeypatch):
    enqueued = collect_enqueued(monkeypatch)

    response = post_transactional(client, calendar_send_payload(dry_run=True, calendar_alternative="broken"))

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"calendar_alternative": "must_be_object"}
    assert enqueued == []
    assert TransactionalMessage.objects.count() == 0
    assert EmailEvent.objects.count() == 0


def test_recipient_list_send_ignores_calendar_alternative(
    client,
    audience,
    api_client_record,
    template,
    monkeypatch,
):
    batches = []
    monkeypatch.setattr(
        "mailing.services.transactional.enqueue_transactional_email_batch",
        lambda message_ids, **kwargs: batches.append(list(message_ids)),
    )
    member_contact = Contact.objects.create(email="learner@example.com")
    recipient_list = RecipientList.objects.create(
        client=api_client_record,
        audience=audience,
        key="course-events:kickoff",
        type="event_attendees",
        name="Kickoff attendees",
        member_count=1,
        active_member_count=1,
    )
    RecipientListMember.objects.create(
        recipient_list=recipient_list,
        contact=member_contact,
        email=member_contact.normalized_email,
        source_object_key="registration:1",
    )

    response = post_recipient_list_transactional(
        client,
        recipient_list.key,
        {
            "audience": audience.slug,
            "client": api_client_record.slug,
            "template_key": template.key,
            "idempotency_key": "kickoff-invite:1",
            "context": {"product": "Datamailer", "verification_url": "https://example.com/verify/token"},
            "calendar_alternative": {"content": CALENDAR_CONTENT, "method": "REQUEST"},
        },
    )

    assert response.status_code == 202
    message = TransactionalMessage.objects.get()
    assert message.status == TransactionalMessageStatus.QUEUED
    assert "calendar_alternative" not in message.metadata
    queue_payload = build_transactional_queue_payload(message)
    assert validate_transactional_email_message(queue_payload) == queue_payload
    assert "calendar_alternative" not in queue_payload["metadata"]
    assert batches == [[message.id]]


def test_transient_recipient_list_send_ignores_calendar_alternative(
    client,
    audience,
    api_client_record,
    template,
    monkeypatch,
):
    batches = []
    monkeypatch.setattr(
        "mailing.services.transactional.enqueue_transactional_email_batch",
        lambda message_ids, **kwargs: batches.append(list(message_ids)),
    )

    response = post_transient_recipient_list_transactional(
        client,
        {
            "audience": audience.slug,
            "client": api_client_record.slug,
            "template_key": template.key,
            "idempotency_key": "kickoff-invite:transient",
            "context": {"product": "Datamailer", "verification_url": "https://example.com/verify/token"},
            "list": {"key": "kickoff-invites", "name": "Kickoff invites"},
            "members": [
                {
                    "source_object_key": "registration:1",
                    "email": "learner@example.com",
                    "status": "active",
                },
            ],
            "calendar_alternative": {"content": CALENDAR_CONTENT, "method": "REQUEST"},
        },
    )

    assert response.status_code == 202
    message = TransactionalMessage.objects.get()
    assert message.status == TransactionalMessageStatus.QUEUED
    assert "calendar_alternative" not in message.metadata
    assert batches == [[message.id]]


def test_calendar_send_replay_returns_existing_identity_without_reenqueue(
    client,
    template,
    monkeypatch,
):
    enqueued = collect_enqueued(monkeypatch)

    first = post_transactional(client, calendar_send_payload())
    replay = post_transactional(client, calendar_send_payload())

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["enqueued"] is False
    assert replay.json()["message"]["id"] == first.json()["message"]["id"]
    assert TransactionalMessage.objects.count() == 1
    assert len(enqueued) == 1


def test_send_without_calendar_keeps_simple_metadata_unchanged(client, template, monkeypatch):
    enqueued = collect_enqueued(monkeypatch)

    payload = calendar_send_payload()
    del payload["calendar_alternative"]
    payload["metadata"] = {"user_id": "42"}
    response = post_transactional(client, payload)

    assert response.status_code == 202
    message = TransactionalMessage.objects.get()
    assert message.metadata == {"user_id": "42"}
    assert "calendar_alternative" not in enqueued[0]["metadata"]


@override_settings(DEFAULT_FROM_EMAIL="sender@example.com", AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="")
def test_worker_without_calendar_still_uses_simple_send_email(transactional_message, monkeypatch):
    ses = boto3.client(
        "ses",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    with Stubber(ses) as stubber:
        stubber.add_response(
            "send_email",
            {"MessageId": "ses-message-123"},
            {
                "Source": "courses@dtcdev.click",
                "Destination": {"ToAddresses": ["person@example.com"]},
                "Message": {
                    "Subject": {"Charset": "UTF-8", "Data": "Persisted subject"},
                    "Body": {
                        "Html": {"Charset": "UTF-8", "Data": "<p>Persisted body</p>"},
                        "Text": {"Charset": "UTF-8", "Data": "Persisted body"},
                    },
                },
            },
        )
        monkeypatch.setattr("mailing.services.transactional_sender.ses_client_for_source", lambda source: ses)

        response = transactional_email_handler(
            _event("message-1", build_transactional_queue_payload(transactional_message))
        )

    transactional_message.refresh_from_db()
    assert response == {"batchItemFailures": []}
    assert transactional_message.status == TransactionalMessageStatus.SENT
    assert transactional_message.ses_message_id == "ses-message-123"


@override_settings(DEFAULT_FROM_EMAIL="sender@example.com", AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="")
def test_worker_with_headers_and_parts_without_calendar_still_uses_send_raw_email(
    transactional_message,
    monkeypatch,
):
    transactional_message.metadata = {
        "headers": {"X-Track": "keep"},
        "message_parts": [
            {
                "content_type": "text/plain",
                "content": "attached note",
                "filename": "note.txt",
                "disposition": "attachment",
            }
        ],
    }
    transactional_message.save(update_fields=["metadata", "updated_at"])
    ses = FakeRawSesClient()
    monkeypatch.setattr("mailing.services.transactional_sender.ses_client_for_source", lambda source: ses)

    response = transactional_email_handler(
        _event("message-1", build_transactional_queue_payload(transactional_message))
    )

    transactional_message.refresh_from_db()
    assert response == {"batchItemFailures": []}
    assert transactional_message.status == TransactionalMessageStatus.SENT
    parsed = message_from_bytes(ses.raw_params["RawMessage"]["Data"])
    assert parsed.get_content_type() == "multipart/mixed"


@override_settings(DEFAULT_FROM_EMAIL="sender@example.com", AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="")
def test_worker_refuses_corrupted_calendar_profile_before_provider_call(transactional_message, monkeypatch):
    transactional_message.metadata = {
        # Simulate a corrupted durable profile: the declared method no longer
        # matches the stored content, so the transport must refuse to build.
        "calendar_alternative": {"content": CALENDAR_CONTENT, "method": "CANCEL", "name": "event.ics"},
    }
    transactional_message.save(update_fields=["metadata", "updated_at"])
    ses = RecordingSesClient()
    monkeypatch.setattr("mailing.services.transactional_sender.ses_client_for_source", lambda source: ses)

    response = transactional_email_handler(
        _event("message-1", build_transactional_queue_payload(transactional_message))
    )

    transactional_message.refresh_from_db()
    # The calendar ValueError from the transport surfaces as a retryable
    # record failure -- not as an ordinary send -- and no provider send
    # method is ever invoked.
    assert response == {"batchItemFailures": [{"itemIdentifier": "message-1"}]}
    assert ses.calls == []
    assert transactional_message.status == TransactionalMessageStatus.SENDING
    assert transactional_message.ses_message_id == ""

    # Positive control: with a valid durable profile the same worker entry
    # does invoke the provider through the factory, so the refusal
    # assertions above can actually fail.
    transactional_message.status = TransactionalMessageStatus.QUEUED
    transactional_message.metadata = {
        "calendar_alternative": {"content": CALENDAR_CONTENT, "method": "REQUEST", "name": "event.ics"},
    }
    transactional_message.save(update_fields=["status", "metadata", "updated_at"])

    response = transactional_email_handler(
        _event("message-2", build_transactional_queue_payload(transactional_message))
    )

    transactional_message.refresh_from_db()
    assert response == {"batchItemFailures": []}
    assert ses.calls == ["send_raw_email"]
    assert transactional_message.status == TransactionalMessageStatus.SENT
    assert transactional_message.ses_message_id == "raw-ses-message-123"


def test_queue_payload_of_calendar_send_round_trips_through_contract(client, api_client_record, template):
    message = TransactionalMessage.objects.create(
        client=api_client_record,
        contact=Contact.objects.create(email="person@example.com"),
        email="person@example.com",
        from_email_id="newsletter",
        from_email="newsletter@example.com",
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.QUEUED,
        idempotency_key="calendar-direct-1",
        subject="Verify Datamailer",
        html_body="<p>Verify</p>",
        text_body="Verify",
        metadata={
            "calendar_alternative": {"content": CALENDAR_CONTENT, "method": "REQUEST", "name": "invite.ics"},
        },
    )

    payload = build_transactional_queue_payload(message)

    assert validate_transactional_email_message(payload) == payload
    assert payload["metadata"]["calendar_alternative"]["method"] == "REQUEST"
