"""Shared helpers for the transactional calendar contract tests.

Nothing here is collected by pytest: the module only exists so the database
fixtures, request helpers, payload builders, SES doubles, and queue/MIME
arrangements stay identical across the calendar API, worker, list-path, and
guard test modules.
"""

import json
from email import message_from_bytes

import pytest
from django.urls import reverse

from mailing.models import (
    Audience,
    Client,
    Contact,
    EmailTemplate,
    Organization,
    RecipientList,
    RecipientListMember,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.auth import create_client_api_key
from mailing.sqs import records_from_messages

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

SMUGGLED_METADATA = {
    "calendar_alternative": {"content": CALENDAR_CONTENT, "method": "REQUEST"},
    "user_id": "42",
}


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


def recipient_list_payload(audience, client_record, template_key, idempotency_key, **extra):
    payload = {
        "audience": audience.slug,
        "client": client_record.slug,
        "template_key": template_key,
        "idempotency_key": idempotency_key,
        "context": {"product": "Datamailer", "verification_url": "https://example.com/verify/token"},
    }
    payload |= extra
    return payload


def transient_recipient_list_payload(audience, client_record, template_key, **extra):
    payload = {
        "audience": audience.slug,
        "client": client_record.slug,
        "template_key": template_key,
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
    }
    payload |= extra
    return payload


def arrange_event_recipient_list(client_record, audience, key, name):
    member_contact = Contact.objects.create(email="learner@example.com")
    recipient_list = RecipientList.objects.create(
        client=client_record,
        audience=audience,
        key=key,
        type="event_attendees",
        name=name,
        member_count=1,
        active_member_count=1,
    )
    RecipientListMember.objects.create(
        recipient_list=recipient_list,
        contact=member_contact,
        email=member_contact.normalized_email,
        source_object_key="registration:1",
    )
    return recipient_list


def collect_enqueued(monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    return enqueued


def collect_enqueued_batches(monkeypatch):
    batches = []
    monkeypatch.setattr(
        "mailing.services.transactional.enqueue_transactional_email_batch",
        lambda message_ids, **kwargs: batches.append(list(message_ids)),
    )
    return batches


class FakeRawSesClient:
    def __init__(self):
        self.raw_params = None

    def send_raw_email(self, **params):
        self.raw_params = params
        return {"MessageId": "raw-ses-message-123"}


class RecordingSesClient:
    """SES double that records every method invocation.

    A no-call assertion against this double is only meaningful together with
    the positive control that observes a real send_raw_email through the same
    recording wiring.
    """

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(**params):
            self.calls.append((name, params))
            return {"MessageId": f"recorded-{name}-1"}

        return record


def _message(message_id, payload):
    return {
        "MessageId": message_id,
        "ReceiptHandle": f"{message_id}-receipt",
        "Body": json.dumps(payload),
    }


def _event(message_id, payload):
    return records_from_messages([_message(message_id, payload)])


def simple_send_expected_params():
    return {
        "Source": "courses@dtcdev.click",
        "Destination": {"ToAddresses": ["person@example.com"]},
        "Message": {
            "Subject": {"Charset": "UTF-8", "Data": "Persisted subject"},
            "Body": {
                "Html": {"Charset": "UTF-8", "Data": "<p>Persisted body</p>"},
                "Text": {"Charset": "UTF-8", "Data": "Persisted body"},
            },
        },
    }


def assert_canonical_calendar_profile(metadata):
    """Assert the durable profile is the validated canonical shape."""
    assert metadata["calendar_alternative"] == {
        "content": CALENDAR_CONTENT,
        "method": "REQUEST",
        "name": "event.ics",
    }


def assert_alternative_mime_contract(raw_message):
    """Parse the outgoing raw bytes and assert the ordered calendar-last alternative contract."""
    parsed = message_from_bytes(raw_message)
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
