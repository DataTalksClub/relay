from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from mailing.forms import CampaignForm
from mailing.models import Audience, Campaign, Client, Organization
from mailing.services.api import queue_campaign_for_client
from mailing.services.campaigns import CampaignNotReady, campaign_send_issues, queue_campaign

pytestmark = pytest.mark.django_db


@pytest.fixture
def scope():
    organization = Organization.objects.create(name="Campaign team", slug="campaign-team")
    client = Client.objects.create(organization=organization, name="Newsletter", slug="newsletter")
    audience = Audience.objects.create(organization=organization, name="Readers", slug="readers")
    return client, audience


def test_save_and_resume_incomplete_draft_without_sending(scope, client):
    active_client, audience = scope
    operator = get_user_model().objects.create_user(username="campaign-editor", is_staff=True)
    client.force_login(operator)
    with patch("mailing.services.campaigns.enqueue_campaign_email") as enqueue:
        response = client.post(reverse("mailing:campaign_create"), {
            "client": active_client.pk, "audience": audience.pk,
            "subject": "", "html_body": "", "text_body": "",
        })
    campaign = Campaign.objects.get()
    assert response.status_code == 302
    assert campaign.status == "draft"
    assert campaign_send_issues(campaign)
    enqueue.assert_not_called()
    detail = client.get(response.url)
    assert detail.status_code == 200
    assert b"Untitled draft" in detail.content
    assert b"Complete draft" in detail.content
    assert b"name=\"confirm\"" not in detail.content
    edit = client.get(reverse("mailing:campaign_edit", args=[campaign.pk]))
    assert edit.status_code == 200


@pytest.mark.parametrize("subject,html,text", [("", "<p>Hello</p>", "Hello"), ("Hello", "", "")])
def test_incomplete_draft_cannot_snapshot_or_enqueue(scope, subject, html, text):
    active_client, audience = scope
    campaign = Campaign.objects.create(client=active_client, audience=audience, subject=subject,
                                       html_body=html, text_body=text)
    with patch("mailing.services.campaigns.snapshot_campaign_recipients") as snapshot:
        with pytest.raises(CampaignNotReady):
            queue_campaign(campaign)
    snapshot.assert_not_called()
    campaign.refresh_from_db()
    assert campaign.status == "draft"
    assert not campaign.recipients.exists()


def test_future_send_time_accepts_schedule_without_snapshotting(scope):
    active_client, audience = scope
    campaign = Campaign.objects.create(client=active_client, audience=audience, external_key="future",
                                       subject="Future", html_body="<p>Hello</p>",
                                       scheduled_at=timezone.now() + timedelta(hours=1))
    with patch("mailing.services.campaigns.snapshot_campaign_recipients") as snapshot:
        result = queue_campaign(campaign)
        assert result.scheduled
        payload = queue_campaign_for_client("future", {"audience": audience.slug, "client": active_client.slug}, active_client)
        assert payload["scheduled"]
    snapshot.assert_not_called()
    campaign.refresh_from_db()
    assert campaign.status == "scheduled"


def test_resaving_legacy_timed_draft_explicitly_clears_time(scope):
    active_client, audience = scope
    campaign = Campaign.objects.create(client=active_client, audience=audience, subject="Draft",
                                       scheduled_at=timezone.now() + timedelta(days=1))
    form = CampaignForm({"client": active_client.pk, "audience": audience.pk, "subject": "Draft",
                         "html_body": "<p>Hello</p>", "text_body": ""},
                        instance=campaign, active_client=active_client)
    assert "scheduled_at" in form.fields
    assert form.is_valid(), form.errors
    form.save()
    campaign.refresh_from_db()
    assert campaign.scheduled_at is None
    assert campaign_send_issues(campaign) == []


def test_review_contains_isolated_actual_rendered_email(scope, client):
    active_client, audience = scope
    operator = get_user_model().objects.create_user(username="campaign-reviewer", is_staff=True)
    client.force_login(operator)
    campaign = Campaign.objects.create(client=active_client, audience=audience,
                                       subject="Review", html_body='<h1>Hello</h1><script>alert(1)</script>',
                                       text_body="Hello")
    response = client.get(reverse("mailing:campaign_detail", args=[campaign.pk]) + "?confirm_send=1")
    assert response.status_code == 200
    html = response.content.decode()
    assert 'sandbox=""' in html
    assert 'srcdoc="' in html
    assert '&lt;h1&gt;Hello&lt;/h1&gt;' in html
    assert '<script>alert(1)</script>' not in html
    assert "Send to 0 recipients now" in html
    assert "Send now after confirmation" in html
