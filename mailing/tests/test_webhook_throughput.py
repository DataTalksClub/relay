"""provider-events consumers: N processes, duplicate deliveries, no double effects.

The tests that race two consumers use real connections and row locks, so they only run on PostgreSQL (``DATABASE_URL=postgres://...``).
"""

import json
import random
import threading

import pytest
from django.db import IntegrityError, connection, connections, transaction
from django.test import override_settings
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    Client,
    CmpCallback,
    Contact,
    EmailEvent,
    Organization,
)
from mailing.services import ses_webhooks
from mailing.services.ses_webhooks import normalize_ses_webhook_worker_payload, process_ses_webhook

pytestmark = pytest.mark.django_db(transaction=True)

CMP = {"CMP_WEBHOOK_URL": "https://cmp.example.test/hook", "CMP_WEBHOOK_TOKEN": "t"}


@pytest.fixture
def campaign():
    org = Organization.objects.create(name="DTC", slug="dtc")
    audience = Audience.objects.create(organization=org, name="DTC", slug="dtc")
    client = Client.objects.create(organization=org, name="C", slug="c")
    return Campaign.objects.create(audience=audience, client=client, subject="S")


def _recipient(campaign, n):
    contact = Contact.objects.create(email=f"p{n}@example.com", verified_at=timezone.now())
    return CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email=contact.email,
        status=CampaignRecipientStatus.SENT,
        ses_message_id=f"ses-{n}",
        sent_at=timezone.now(),
    )


def _sns(event_id, message_id, event_name):
    ts = "2026-11-09T10:00:00.000Z"
    ses = {
        "eventType": event_name,
        "mail": {"messageId": message_id, "timestamp": ts},
        event_name.lower(): {"timestamp": ts},
    }
    return {
        "Type": "Notification",
        "MessageId": event_id,
        "TopicArn": "arn:aws:sns:eu-west-1:000000000000:t",
        "Timestamp": ts,
        "Message": json.dumps(ses),
    }


def _apply(sns_payload):
    return process_ses_webhook(normalize_ses_webhook_worker_payload(sns_payload))


@override_settings(**CMP)
def test_cmp_callback_commits_atomically_with_the_event(campaign):
    recipient = _recipient(campaign, 1)

    class Boom(Exception):
        pass

    with pytest.raises(Boom), transaction.atomic():
        _apply(_sns("00000000-0000-0000-0000-000000000001", recipient.ses_message_id, "Delivery"))
        assert CmpCallback.objects.count() == 1  # written inside the transaction, not on commit
        raise Boom

    assert EmailEvent.objects.count() == 0
    assert CmpCallback.objects.count() == 0

    event = _apply(_sns("00000000-0000-0000-0000-000000000001", recipient.ses_message_id, "Delivery"))
    assert event is not None
    assert CmpCallback.objects.filter(email_event=event).count() == 1


@pytest.mark.skipif(connection.vendor != "postgresql", reason="needs a second committing connection")
def test_losing_a_duplicate_race_is_a_noop_not_a_failure(campaign, monkeypatch, caplog):
    """Another consumer commits the same provider_event_id between our pre-check and INSERT."""
    recipient = _recipient(campaign, 1)
    event_id = "00000000-0000-0000-0000-0000000000aa"
    real_correlate = ses_webhooks.correlate_ses_message

    def other_consumer_commits():
        try:
            EmailEvent.objects.create(event_type="open", provider_event_id=event_id, campaign=campaign)
        finally:
            connections.close_all()

    def correlate_then_lose_the_race(ses_message_id):
        source = real_correlate(ses_message_id)
        # A separate connection, so the competing row is committed independently
        # of (and survives the rollback of) this consumer's transaction.
        other = threading.Thread(target=other_consumer_commits)
        other.start()
        other.join(timeout=30)
        return source

    monkeypatch.setattr(ses_webhooks, "correlate_ses_message", correlate_then_lose_the_race)
    with caplog.at_level("INFO", logger=ses_webhooks.logger.name):
        assert _apply(_sns(event_id, recipient.ses_message_id, "Open")) is None

    recipient.refresh_from_db()
    assert recipient.open_count == 0  # the loser applied nothing
    assert EmailEvent.objects.filter(provider_event_id=event_id).count() == 1
    assert "ses_event_duplicate" in caplog.text


def test_other_integrity_errors_still_fail_loudly(campaign, monkeypatch):
    recipient = _recipient(campaign, 1)

    def broken_append(payload, source):
        raise IntegrityError("some other constraint")

    monkeypatch.setattr(ses_webhooks, "append_provider_event", broken_append)
    with pytest.raises(IntegrityError, match="some other constraint"):
        _apply(_sns("00000000-0000-0000-0000-0000000000bb", recipient.ses_message_id, "Delivery"))


@pytest.mark.skipif(connection.vendor != "postgresql", reason="needs real row locks and concurrent connections")
@override_settings(**CMP)
def test_concurrent_consumers_apply_every_event_exactly_once(campaign, settings):
    settings.CMP_WEBHOOK_URL = CMP["CMP_WEBHOOK_URL"]
    settings.CMP_WEBHOOK_TOKEN = CMP["CMP_WEBHOOK_TOKEN"]
    recipients = [_recipient(campaign, n) for n in range(40)]
    events = []
    n = 0
    for r in recipients:
        # One delivery, two distinct opens, one click per recipient: several events
        # race on the same recipient row, plus every event is delivered three times.
        for name in ("Delivery", "Open", "Open", "Click"):
            n += 1
            events.append(_sns(f"00000000-0000-0000-0000-{n:012d}", r.ses_message_id, name))
    stream = [e for e in events for _ in range(3)]
    random.Random(3).shuffle(stream)

    consumers = 4
    chunks = [stream[i::consumers] for i in range(consumers)]
    errors = []
    barrier = threading.Barrier(consumers)

    def consume(chunk):
        try:
            barrier.wait()
            for payload in chunk:
                _apply(payload)
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(repr(exc))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=consume, args=(c,)) for c in chunks]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not any(t.is_alive() for t in threads), "consumers hung"
    assert errors == []

    assert EmailEvent.objects.count() == len(events)
    assert EmailEvent.objects.values("provider_event_id").distinct().count() == len(events)
    for r in CampaignRecipient.objects.filter(campaign=campaign):
        assert r.delivered_at is not None
        assert r.open_count == 2
        assert r.click_count == 1
    # One outbox row per applied event, never per delivery.
    assert CmpCallback.objects.count() == len(events)
