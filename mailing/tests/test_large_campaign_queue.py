"""Large send-now queues must not run inside the web request (campaign 318, 2026-11-23).

Prod: queueing DTC Weekly #304 (~131k recipients) failed twice with a generic
"request failed while accessing the database" because the per-recipient
QUEUED INSERT ... SELECT hit the 15 s web statement_timeout.
"""
import json

import pytest
from django.contrib.auth import get_user_model
from django.db import OperationalError
from django.test import Client as HttpClient
from django.test import RequestFactory, override_settings
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from mailing.middleware import database_error_response, is_database_statement_timeout
from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    Organization,
    Subscription,
)
from mailing.services import campaigns
from mailing.services.admin_auth import create_admin_api_key
from mailing.services.campaigns import (
    dispatch_due_campaigns,
    queue_campaign,
    record_batch_queued_events,
    request_campaign_queue,
)

pytestmark = pytest.mark.django_db


class StatementTimeout(OperationalError):
    pgcode = "57014"


@pytest.fixture
def campaign():
    org = Organization.objects.create(name="Large", slug="large")
    client = Client.objects.create(organization=org, name="Weekly", slug="weekly")
    audience = Audience.objects.create(organization=org, name="Readers", slug="readers")
    for i in range(5):
        contact = Contact.objects.create(email=f"reader{i}@example.com", verified_at=timezone.now())
        Subscription.objects.create(contact=contact, audience=audience, client=client, status="subscribed")
    return Campaign.objects.create(client=client, audience=audience, subject="Weekly", html_body="<h1>Hi</h1>")


@override_settings(CAMPAIGN_SYNC_QUEUE_MAX_RECIPIENTS=3)
def test_large_send_now_is_handed_to_scheduler_without_snapshot(campaign):
    result = request_campaign_queue(campaign)

    campaign.refresh_from_db()
    assert result.accepted is True
    assert result.snapshotting is True
    assert result.already_queued is False
    assert campaign.status == CampaignStatus.SCHEDULED
    assert campaign.scheduled_at is not None
    assert campaign.metadata["queue_handoff"]["candidate_count"] == 5
    assert CampaignRecipient.objects.filter(campaign=campaign).count() == 0

    # The scheduler's next pass queues it with the normal queue_campaign path.
    dispatched = dispatch_due_campaigns()
    campaign.refresh_from_db()
    assert [r.campaign_id for r in dispatched] == [campaign.id]
    assert campaign.status == CampaignStatus.QUEUED
    assert CampaignRecipient.objects.filter(campaign=campaign, status="pending").count() == 5

    # A repeated request does not queue twice.
    again = request_campaign_queue(campaign)
    assert again.accepted is False and again.already_queued is True


@override_settings(CAMPAIGN_SYNC_QUEUE_MAX_RECIPIENTS=10)
def test_small_send_now_stays_synchronous(campaign):
    result = request_campaign_queue(campaign)
    campaign.refresh_from_db()
    assert result.accepted is True and result.snapshotting is False
    assert campaign.status == CampaignStatus.QUEUED
    assert CampaignRecipient.objects.filter(campaign=campaign).count() == 5


def test_queue_writes_no_per_recipient_events_in_request(campaign):
    queue_campaign(campaign)
    assert not EmailEvent.objects.filter(campaign=campaign, event_type=EmailEventType.QUEUED).exists()


def test_batch_queued_events_are_written_once_per_recipient(campaign):
    queue_campaign(campaign)
    ids = list(CampaignRecipient.objects.filter(campaign=campaign).values_list("id", flat=True))

    assert record_batch_queued_events(campaign, ids[:2], "b1") == 2
    assert record_batch_queued_events(campaign, ids, "b1") == 3  # redelivery: only the missing ones
    assert record_batch_queued_events(campaign, ids, "b1") == 0
    events = EmailEvent.objects.filter(campaign=campaign, event_type=EmailEventType.QUEUED)
    assert events.count() == 5
    assert all(e.metadata == {"batch_id": "b1"} for e in events)


def test_statement_timeout_is_a_clear_campaign_error_and_rolls_back(campaign, monkeypatch):
    def timeout(_campaign):
        raise StatementTimeout("canceling statement due to statement timeout")

    monkeypatch.setattr(campaigns, "snapshot_campaign_recipients", timeout)
    user = get_user_model().objects.create_user("operator", is_staff=True)
    _, token = create_admin_api_key(user=user, name="operator")
    http = HttpClient(HTTP_AUTHORIZATION=f"Bearer {token}")

    response = http.post(
        f"/api/admin/campaigns/{campaign.id}/queue", data=json.dumps({"confirm": True}),
        content_type="application/json",
    )

    assert response.status_code == 504
    error = response.json()["error"]
    assert error["code"] == "campaign_queue_timeout"
    assert f"campaign {campaign.id}" in error["fields"]["campaign"]
    assert "rolled back" in error["fields"]["campaign"]
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.DRAFT
    assert not DBTaskResult.objects.exists()


def test_middleware_reports_statement_timeout_distinctly():
    exc = StatementTimeout("canceling statement due to statement timeout")
    assert is_database_statement_timeout(exc)
    response = database_error_response(RequestFactory().post("/api/admin/x"), exc)
    assert response.status_code == 504
    body = json.loads(response.content)
    assert body["error"]["code"] == "database_statement_timeout"
    assert response["X-Relay-Error-Reason"] == "database_statement_timeout"
    assert not is_database_statement_timeout(OperationalError("server closed the connection unexpectedly"))


def test_operator_key_on_client_endpoint_names_the_admin_path(campaign):
    user = get_user_model().objects.create_user("operator2", is_staff=True)
    _, token = create_admin_api_key(user=user, name="operator")
    http = HttpClient(HTTP_AUTHORIZATION=f"Bearer {token}")

    response = http.post("/api/newsletter-issues/weekly-304/assemble", data="{}", content_type="application/json")

    assert response.status_code == 401
    message = response.json()["error"]["message"]
    assert "/api/admin/clients/<client_id>/newsletter-issues/weekly-304/assemble" in message
