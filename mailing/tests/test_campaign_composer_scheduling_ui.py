import json
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from mailing.forms import CampaignForm
from mailing.models import Audience, Campaign, Client, Organization
from mailing.services.campaigns import campaign_send_issues

pytestmark = pytest.mark.django_db


@pytest.fixture
def scope():
    organization = Organization.objects.create(name="Composer team", slug="composer-team")
    client = Client.objects.create(organization=organization, name="News", slug="news")
    audience = Audience.objects.create(organization=organization, name="Readers", slug="readers")
    return client, audience


def form_for(scope, **values):
    client, audience = scope
    data = {"client": client.pk, "audience": audience.pk, "subject": "Announcement",
            "html_body": "<p>Source version</p>", "text_body": "Source version", **values}
    return CampaignForm(data, active_client=client)


def test_composer_persists_blocks_and_generates_safe_html_and_text(scope):
    blocks = [{"type": "heading", "text": '<script>alert("unsafe")</script>'},
              {"type": "paragraph", "text": "Hello\nSubscribers"},
              {"type": "button", "text": "Register & learn", "url": "https://example.com/course?a=1&b=2"}]
    form = form_for(scope, editor_mode="blocks", editor_blocks=json.dumps(blocks))
    assert form.is_valid(), form.errors
    campaign = form.save()
    assert '<script>' not in campaign.html_body
    assert '&lt;script&gt;' in campaign.html_body
    assert 'href="https://example.com/course?a=1&amp;b=2"' in campaign.html_body
    assert "Hello\nSubscribers" in campaign.text_body
    assert "Register & learn: https://example.com/course?a=1&b=2" in campaign.text_body
    assert campaign.metadata["operator_editor"]["blocks"] == blocks
    resumed = CampaignForm(instance=campaign, active_client=scope[0])
    assert resumed.initial["editor_mode"] == "blocks"
    assert json.loads(resumed.initial["editor_blocks"]) == blocks


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,unsafe", "https://[bad", "//example.com", "https://user:secret@example.com", "https://example.com/\nunsafe"])
def test_composer_rejects_unsafe_button_destination_without_saving(scope, url):
    form = form_for(scope, editor_mode="blocks", editor_blocks=json.dumps([
        {"type": "button", "text": "Open", "url": url}]))
    assert not form.is_valid()
    assert "editor_blocks" in form.errors
    assert not Campaign.objects.exists()


def test_incomplete_visual_draft_saves_but_cannot_send(scope):
    form = form_for(scope, editor_mode="blocks", editor_blocks=json.dumps([
        {"type": "paragraph", "text": "Still drafting"}, {"type": "button", "text": "", "url": ""}]))
    assert form.is_valid(), form.errors
    campaign = form.save()
    assert any("button" in issue for issue in campaign_send_issues(campaign))
    assert campaign.status == "draft"
    assert not campaign.recipients.exists()


def test_source_edit_preserves_external_metadata_and_clears_obsolete_blocks(scope):
    client, audience = scope
    campaign = Campaign.objects.create(client=client, audience=audience, metadata={
        "external": {"owner": "course-app"}, "operator_editor": {"blocks": [{"type":"heading", "text":"Old"}]}})
    form = CampaignForm({"client":client.pk, "audience":audience.pk, "subject":"Imported",
                         "html_body":"<p>Imported exactly</p>", "text_body":"Imported exactly", "editor_mode":"source"},
                        instance=campaign, active_client=client)
    assert form.is_valid(), form.errors
    form.save()
    campaign.refresh_from_db()
    assert campaign.html_body == "<p>Imported exactly</p>"
    assert campaign.metadata["external"] == {"owner":"course-app"}
    assert "operator_editor" not in campaign.metadata


def test_scheduled_local_time_round_trip_uses_explicit_zone(scope):
    form = form_for(scope, send_mode="later", scheduled_at="2027-01-10T09:00", schedule_timezone="America/New_York")
    assert form.is_valid(), form.errors
    campaign = form.save()
    assert campaign.scheduled_at == datetime(2027,1,10,14,tzinfo=dt_timezone.utc)
    resumed = CampaignForm(instance=campaign, active_client=scope[0])
    assert resumed.initial["scheduled_at"] == "2027-01-10T09:00"
    assert resumed.initial["schedule_timezone"] == "America/New_York"
    assert resumed.initial["send_mode"] == "later"


@pytest.mark.parametrize("local,zone", [("2027-03-28T02:30","Europe/Berlin"), ("2027-10-31T02:30","Europe/Berlin"),
                                       ("2027-01-10T09:00","Not/AZone"), ("2027-01-10T09:00+03:00","UTC")])
def test_invalid_or_ambiguous_wall_time_requires_correction(scope, local, zone):
    form = form_for(scope, send_mode="later", scheduled_at=local, schedule_timezone=zone)
    assert not form.is_valid()
    assert "scheduled_at" in form.errors
    assert not Campaign.objects.exists()


def test_switching_to_send_now_clears_saved_schedule(scope):
    client, audience = scope
    campaign = Campaign.objects.create(client=client, audience=audience, scheduled_at=timezone.now()+timedelta(days=1))
    form = CampaignForm({"client":client.pk, "audience":audience.pk, "subject":"Now", "html_body":"Hello", "send_mode":"now"},
                        instance=campaign, active_client=client)
    assert form.is_valid(), form.errors
    form.save()
    campaign.refresh_from_db()
    assert campaign.scheduled_at is None


def test_operator_review_commits_schedule_without_queueing_recipients(scope, client):
    active_client, audience = scope
    client.force_login(get_user_model().objects.create_user(username="scheduler-review",is_staff=True))
    campaign = Campaign.objects.create(client=active_client,audience=audience,subject="Future",html_body="<p>Hello</p>",
                                       scheduled_at=timezone.now()+timedelta(days=1),metadata={"operator_schedule_timezone":"Europe/Berlin"})
    detail = reverse("mailing:campaign_detail",args=[campaign.pk])
    review = client.get(detail+"?confirm_send=1")
    assert b"Schedule this campaign?" in review.content
    assert b"Europe/Berlin" in review.content
    assert b"final audience is selected at dispatch time" in review.content
    with patch("mailing.services.campaigns.enqueue_campaign_email") as enqueue:
        result = client.post(reverse("mailing:campaign_queue",args=[campaign.pk]),
                             {"confirm":"1","reviewed_recipient_count":"0","operator_client_id":active_client.pk})
    assert result.status_code == 302
    enqueue.assert_not_called()
    campaign.refresh_from_db()
    assert campaign.status == "scheduled"
    assert not campaign.recipients.exists()
    page = client.get(detail)
    assert b"Scheduled for" in page.content
    assert b"Confirm cancellation" in page.content
    assert b'id="campaign-stats"' not in page.content


@pytest.mark.parametrize("change", ["content", "expired-time"])
def test_old_operator_review_cannot_send_changed_or_expired_draft(scope, client, change):
    active_client, audience = scope
    client.force_login(get_user_model().objects.create_user(username=f"stale-{change}", is_staff=True))
    campaign = Campaign.objects.create(client=active_client, audience=audience, subject="Reviewed", html_body="Hello",
                                       scheduled_at=timezone.now()+timedelta(hours=1))
    revision = campaign.updated_at.isoformat()
    if change == "content":
        campaign.subject = "Changed after review"
        campaign.save()
    else:
        Campaign.objects.filter(pk=campaign.pk).update(scheduled_at=timezone.now()-timedelta(minutes=1))
    with patch("mailing.services.campaigns.enqueue_campaign_email") as enqueue:
        response = client.post(reverse("mailing:campaign_queue", args=[campaign.pk]),
                               {"confirm":"1", "reviewed_recipient_count":"0", "review_revision":revision}, follow=True)
    assert response.status_code == 200
    campaign.refresh_from_db()
    assert campaign.status == "draft"
    assert not campaign.recipients.exists()
    enqueue.assert_not_called()
    assert b'role="alert"' in response.content
