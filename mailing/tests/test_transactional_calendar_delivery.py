"""Contract tests for the authenticated single-recipient calendar API family.

Covers the authorized Relay #37 slice: the authenticated single-recipient API
request carries the accepted calendar profile through the durable message
metadata and the queue payload, with request-time validation, dry-run, replay
identity, and the queue-payload contract. The worker transport observability
family lives in test_transactional_calendar_worker.py; the outgoing raw bytes
are parsed and compared as decoded full content there and in the happy path
below, never as builder internals or file extensions.
"""

import pytest
from django.test import override_settings

from mailing.calendar_mime import MAX_CALENDAR_CONTENT_LENGTH
from mailing.models import (
    Contact,
    EmailEvent,
    EmailEventType,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.queue_contracts import validate_transactional_email_message
from mailing.services.transactional import build_transactional_queue_payload
from mailing.tests.transactional_calendar_helpers import (
    CALENDAR_CONTENT,
    FakeRawSesClient,
    _event,
    assert_alternative_mime_contract,
    assert_canonical_calendar_profile,
    calendar_send_payload,
    collect_enqueued,
    post_transactional,
)

# Shared fixtures re-exported under their own names for pytest collection.
from mailing.tests.transactional_calendar_helpers import (
    api_client_record as api_client_record,
)
from mailing.tests.transactional_calendar_helpers import (
    organization as organization,
)
from mailing.tests.transactional_calendar_helpers import (
    template as template,
)
from mailing.workers import transactional_email_handler

pytestmark = pytest.mark.django_db(transaction=True)


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
    assert_canonical_calendar_profile(message.metadata)
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

    assert_alternative_mime_contract(ses.raw_params["RawMessage"]["Data"])


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


def test_calendar_send_replay_returns_existing_identity_without_reenqueue(client, template, monkeypatch):
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
