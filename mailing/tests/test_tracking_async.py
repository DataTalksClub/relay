"""Recipient-facing paths never do lock-heavy work inline (2026-11-09 503 incident).

/t/o and /t/c: one token lookup + one task insert, no row locks, no campaign
query; the tracking worker persists the event. /unsubscribe: durable
synchronous write with a bounded lock wait, or a durable deferred task.
/webhooks/ses: capped per process so a burst cannot take every web thread.
"""

import json
import threading
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
from django.db import DatabaseError, OperationalError
from django.db.models.query import QuerySet
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from mailing import tasks, views
from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    Organization,
    Subscription,
    SubscriptionStatus,
)
from mailing.services import queue_monitor, tracking
from mailing.services.public_urls import click_redirect_url
from mailing.services.tokens import ensure_campaign_recipient_tokens
from mailing.tests.task_helpers import drain_queue

pytestmark = pytest.mark.django_db


def _signed_click_params(token, destination):
    return parse_qs(urlparse(click_redirect_url(token, destination)).query)


@pytest.fixture
def recipient():
    organization = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    audience = Audience.objects.create(organization=organization, name="DataTalksClub", slug="datatalks-club")
    app_client = Client.objects.create(organization=organization, name="Newsletter", slug="newsletter")
    contact = Contact.objects.create(email="reader@example.com", verified_at=timezone.now())
    Subscription.objects.create(contact=contact, audience=audience, status=SubscriptionStatus.SUBSCRIBED)
    campaign = Campaign.objects.create(audience=audience, client=app_client, subject="Weekly")
    return CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email=contact.email,
        status=CampaignRecipientStatus.SENT,
        sent_at=timezone.now(),
    )


@pytest.fixture
def no_row_locks(monkeypatch):
    locked = []
    original = QuerySet._fetch_all

    def fetch(query):
        if query._result_cache is None and query.query.select_for_update:
            locked.append(query.model)
        return original(query)

    monkeypatch.setattr(QuerySet, "_fetch_all", fetch)
    return locked


@pytest.mark.parametrize("kind", ["open", "click"])
def test_tracking_web_path_is_lookup_plus_insert_only(client, recipient, no_row_locks, django_assert_max_num_queries, kind):
    token = ensure_campaign_recipient_tokens(recipient).tracking_token
    no_row_locks.clear()  # token issuance (send time) locks; the request must not
    # 1 token lookup + the task insert (+ taskdeck's projection rows); never a campaign query.
    with django_assert_max_num_queries(8) as captured:
        if kind == "open":
            response = client.get(reverse("mailing:tracking_open", args=[token]))
        else:
            response = client.get(
                reverse("mailing:tracking_click", args=[token]), _signed_click_params(token, "https://example.com/a")
            )

    assert response.status_code == (200 if kind == "open" else 302)
    assert no_row_locks == []
    assert not any('"campaigns"' in query["sql"] for query in captured.captured_queries)
    assert EmailEvent.objects.count() == 0
    task = DBTaskResult.objects.get()
    assert task.queue_name == "tracking"
    assert task.task_path == "mailing.tasks.process_tracking_event"


def test_click_still_redirects_when_enqueue_fails(client, recipient, caplog):
    token = ensure_campaign_recipient_tokens(recipient).tracking_token
    broken = mock.Mock(**{"enqueue.side_effect": DatabaseError("down")})
    with mock.patch("mailing.tasks.process_tracking_event", broken):
        response = client.get(
            reverse("mailing:tracking_click", args=[token]), _signed_click_params(token, "https://example.com/a")
        )

    assert response.status_code == 302
    assert response.headers["Location"] == "https://example.com/a"
    assert "tracking_event_enqueue_failed" in caplog.text


def test_worker_uses_request_time_and_counters_are_exact_after_drain(client, recipient):
    token = ensure_campaign_recipient_tokens(recipient).tracking_token
    before = timezone.now()
    for _ in range(3):
        client.get(reverse("mailing:tracking_open", args=[token]))
    client.get(
        reverse("mailing:tracking_click", args=[token]), _signed_click_params(token, "https://example.com/a")
    )

    assert drain_queue("tracking") == 4

    recipient.refresh_from_db()
    campaign = Campaign.objects.get(pk=recipient.campaign_id)
    assert before <= recipient.first_opened_at <= timezone.now()
    assert (recipient.open_count, recipient.click_count) == (3, 1)
    assert (campaign.open_count, campaign.unique_open_count) == (3, 1)
    assert (campaign.click_count, campaign.unique_click_count) == (1, 1)
    assert EmailEvent.objects.filter(event_type=EmailEventType.OPEN).count() == 3


def test_tracking_event_for_deleted_recipient_is_a_noop(client, recipient):
    token = ensure_campaign_recipient_tokens(recipient).tracking_token
    client.get(reverse("mailing:tracking_open", args=[token]))
    Campaign.objects.filter(pk=recipient.campaign_id).delete()

    assert drain_queue("tracking") == 1
    assert EmailEvent.objects.count() == 0


def test_unsubscribe_never_locks_the_campaign_row(client, recipient, no_row_locks):
    token = ensure_campaign_recipient_tokens(recipient).unsubscribe_token
    no_row_locks.clear()
    response = client.post(reverse("mailing:public_unsubscribe", args=[token]))

    assert response.status_code == 200
    assert no_row_locks[0] is CampaignRecipient
    assert Campaign not in no_row_locks
    recipient.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.UNSUBSCRIBED


def test_unsubscribe_lock_timeout_defers_to_a_durable_task(client, recipient, caplog):
    token = ensure_campaign_recipient_tokens(recipient).unsubscribe_token
    real = tracking.apply_unsubscribe_for_recipient

    def contended(recipient_id, occurred_at, *, lock_timeout_ms=None):
        if lock_timeout_ms:
            raise OperationalError("canceling statement due to lock timeout")
        return real(recipient_id, occurred_at)

    with mock.patch.object(tracking, "apply_unsubscribe_for_recipient", side_effect=contended):
        response = client.post(reverse("mailing:public_unsubscribe", args=[token]))
        assert response.status_code == 200
        assert "You're unsubscribed." in response.content.decode()
        assert "unsubscribe_deferred" in caplog.text
        task = DBTaskResult.objects.get(task_path="mailing.tasks.apply_deferred_unsubscribe")
        assert task.queue_name == "tracking"
        recipient.refresh_from_db()
        assert recipient.status == CampaignRecipientStatus.SENT

        drain_queue("tracking")

    recipient.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.UNSUBSCRIBED
    subscription = Subscription.objects.get(contact=recipient.contact, audience=recipient.campaign.audience)
    assert subscription.status == SubscriptionStatus.UNSUBSCRIBED


@pytest.mark.parametrize("one_click", [False, True])
def test_unsubscribe_that_cannot_be_recorded_says_so(client, recipient, one_click):
    token = ensure_campaign_recipient_tokens(recipient).unsubscribe_token
    data = {"List-Unsubscribe": "One-Click"} if one_click else {}
    with (
        mock.patch.object(tracking, "apply_unsubscribe_for_recipient", side_effect=OperationalError("lock timeout")),
        mock.patch(
            "mailing.tasks.apply_deferred_unsubscribe", mock.Mock(**{"enqueue.side_effect": OperationalError("down")})
        ),
    ):
        response = client.post(reverse("mailing:public_unsubscribe", args=[token]), data)

    assert response.status_code == 503
    if not one_click:
        html = response.content.decode()
        assert "couldn't record your unsubscribe" in html
        assert "You're unsubscribed." not in html


@override_settings(SES_WEBHOOKS_SIGNATURE_MODE="mock")
def test_ses_webhook_is_capped_per_process(client, monkeypatch, caplog):
    monkeypatch.setattr(views, "_ses_webhook_slots", threading.BoundedSemaphore(1))
    views._ses_webhook_slots.acquire()  # one request already in flight
    try:
        response = client.post(reverse("mailing:ses_webhook"), data=json.dumps({}), content_type="application/json")
    finally:
        views._ses_webhook_slots.release()

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.headers["X-Relay-503-Reason"] == "webhook_busy"
    assert '"event":"http_5xx"' in caplog.text
    assert '"reason":"webhook_busy"' in caplog.text
    assert not DBTaskResult.objects.exists()


def test_queue_monitor_alerts_on_stalled_tracking_queue_and_failed_unsubscribe(recipient):
    now = timezone.now()
    tasks.process_tracking_event.enqueue({"kind": "open", "recipient_id": recipient.pk, "occurred_at": now.isoformat()})
    DBTaskResult.objects.update(enqueued_at=now - timedelta(minutes=10))
    failed = tasks.apply_deferred_unsubscribe.enqueue({"recipient_id": recipient.pk, "occurred_at": now.isoformat()})
    DBTaskResult.objects.filter(pk=failed.id).update(status="FAILED")

    with mock.patch.object(queue_monitor, "send_monitor_alert") as send:
        queue_monitor._last_alert_at.clear()
        sent = queue_monitor.check_queue_age(now=now, force=True)
        again = queue_monitor.check_queue_age(now=now + timedelta(minutes=5), force=True)

    kinds = {(alert["kind"], alert["queue"]) for alert in sent}
    assert kinds == {("queue_age", "tracking"), ("deferred_unsubscribe_failed", "tracking")}
    stalled = next(alert for alert in sent if alert["kind"] == "queue_age")
    assert stalled["oldest_ready_age_seconds"] >= 600
    assert again == []  # one alert per queue per hour
    assert send.call_count == 2


def test_queue_monitor_is_quiet_when_queues_drain(recipient):
    with mock.patch.object(queue_monitor, "send_monitor_alert") as send:
        queue_monitor._last_alert_at.clear()
        assert queue_monitor.check_queue_age(force=True) == []
    send.assert_not_called()
