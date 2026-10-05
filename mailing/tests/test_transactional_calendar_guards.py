"""Anti-smuggling and post-render guard regressions for the calendar slice.

One cohesive parent-red target: a client-supplied generic
metadata.calendar_alternative key is stripped from durable metadata on both
send paths while other metadata and the validated top-level field stay
authoritative, and the rendered artifact is revalidated before any durable
effect.
"""

import pytest

from mailing.models import Contact, EmailEvent, TransactionalMessage
from mailing.tests.transactional_calendar_helpers import (
    SMUGGLED_METADATA,
    arrange_event_recipient_list,
    assert_canonical_calendar_profile,
    calendar_send_payload,
    collect_enqueued,
    collect_enqueued_batches,
    post_recipient_list_transactional,
    post_transactional,
    recipient_list_payload,
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


def test_generic_metadata_calendar_alternative_is_stripped_from_single_recipient_send(client, template, monkeypatch):
    enqueued = collect_enqueued(monkeypatch)

    smuggled_payload = calendar_send_payload(idempotency_key="smuggled-only", metadata=SMUGGLED_METADATA)
    del smuggled_payload["calendar_alternative"]
    smuggled = post_transactional(client, smuggled_payload)
    conflicting = post_transactional(
        client,
        calendar_send_payload(
            idempotency_key="smuggled-conflict",
            metadata={"calendar_alternative": {"content": "not a calendar", "method": "REQUEST"}},
        ),
    )

    assert smuggled.status_code == 202
    assert conflicting.status_code == 202
    persisted = list(TransactionalMessage.objects.order_by("id"))
    # Without a top-level field the generic key is dropped, other metadata is
    # untouched, and no unvalidated profile reaches the queue payload.
    assert "calendar_alternative" not in persisted[0].metadata
    assert persisted[0].metadata["user_id"] == "42"
    assert "calendar_alternative" not in enqueued[0]["metadata"]
    # With a top-level field the validated canonical profile stays
    # authoritative over any smuggled generic key.
    assert_canonical_calendar_profile(persisted[1].metadata)
    assert len(enqueued) == 2


def test_generic_metadata_calendar_alternative_is_stripped_from_recipient_list_send(
    client, audience, api_client_record, template, monkeypatch
):
    batches = collect_enqueued_batches(monkeypatch)
    recipient_list = arrange_event_recipient_list(
        api_client_record, audience, key="course-events:smuggled", name="Smuggled attendees"
    )

    response = post_recipient_list_transactional(
        client,
        recipient_list.key,
        recipient_list_payload(
            audience,
            api_client_record,
            template.key,
            "kickoff-invite:smuggled",
            metadata=SMUGGLED_METADATA,
        ),
    )

    assert response.status_code == 202
    message = TransactionalMessage.objects.get()
    assert "calendar_alternative" not in message.metadata
    assert message.metadata["user_id"] == "42"
    assert batches == [[message.id]]


def test_post_render_calendar_revalidation_rejects_unrenderable_bodies_before_persisting(client, template, monkeypatch):
    enqueued = collect_enqueued(monkeypatch)

    response = post_transactional(
        client,
        calendar_send_payload(
            idempotency_key="calendar-post-render",
            context={
                "product": "Datamailer",
                # A lone surrogate survives JSON parsing and request-time
                # validation (which runs against empty stand-in bodies), but
                # the rendered text/HTML can never be UTF-8 encoded, so the
                # final artifact must fail post-render revalidation.
                "verification_url": "https://example.com/verify/\ud800",
            },
        ),
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"calendar_alternative": "invalid"}
    assert Contact.objects.count() == 0
    assert TransactionalMessage.objects.count() == 0
    assert EmailEvent.objects.count() == 0
    assert enqueued == []
