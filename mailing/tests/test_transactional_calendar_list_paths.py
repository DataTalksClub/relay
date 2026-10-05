"""Recipient-list path calendar-neutrality tests for the transactional slice.

The recipient-list and transient-list transactional sends carry no top-level
calendar field and must stay calendar-neutral: a supplied
calendar_alternative never reaches durable metadata or the queue payload.
"""

import pytest

from mailing.models import TransactionalMessage, TransactionalMessageStatus
from mailing.queue_contracts import validate_transactional_email_message
from mailing.services.transactional import build_transactional_queue_payload
from mailing.tests.transactional_calendar_helpers import (
    CALENDAR_CONTENT,
    arrange_event_recipient_list,
    collect_enqueued_batches,
    post_recipient_list_transactional,
    post_transient_recipient_list_transactional,
    recipient_list_payload,
    transient_recipient_list_payload,
)

# Shared fixtures re-exported under their own names for pytest collection.
from mailing.tests.transactional_calendar_helpers import (
    api_client_record as api_client_record,
)
from mailing.tests.transactional_calendar_helpers import (
    audience as audience,
)
from mailing.tests.transactional_calendar_helpers import (
    organization as organization,
)
from mailing.tests.transactional_calendar_helpers import (
    template as template,
)

pytestmark = pytest.mark.django_db(transaction=True)


def test_recipient_list_send_ignores_calendar_alternative(client, audience, api_client_record, template, monkeypatch):
    batches = collect_enqueued_batches(monkeypatch)
    recipient_list = arrange_event_recipient_list(
        api_client_record, audience, key="course-events:kickoff", name="Kickoff attendees"
    )

    response = post_recipient_list_transactional(
        client,
        recipient_list.key,
        recipient_list_payload(
            audience,
            api_client_record,
            template.key,
            "kickoff-invite:1",
            calendar_alternative={"content": CALENDAR_CONTENT, "method": "REQUEST"},
        ),
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
    client, audience, api_client_record, template, monkeypatch
):
    batches = collect_enqueued_batches(monkeypatch)

    response = post_transient_recipient_list_transactional(
        client,
        transient_recipient_list_payload(
            audience,
            api_client_record,
            template.key,
            calendar_alternative={"content": CALENDAR_CONTENT, "method": "REQUEST"},
        ),
    )

    assert response.status_code == 202
    message = TransactionalMessage.objects.get()
    assert message.status == TransactionalMessageStatus.QUEUED
    assert "calendar_alternative" not in message.metadata
    assert batches == [[message.id]]
