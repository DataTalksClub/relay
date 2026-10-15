from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch

import pytest
from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignDispatch,
    CampaignRecipient,
    Client,
    Contact,
    Organization,
    Subscription,
)
from mailing.services.api import cancel_campaign_instance, validate_campaign_payload
from mailing.services.api_errors import ApiValidationError
from mailing.services.campaign_sender import RetryableCampaignSendError, send_campaign_batch
from mailing.services.campaigns import (
    CampaignNotReady,
    dispatch_due_campaigns,
    queue_campaign,
    recover_campaign_dispatches,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def campaign():
    org = Organization.objects.create(name="Schedule", slug="schedule")
    client = Client.objects.create(organization=org, name="Newsletter", slug="newsletter")
    audience = Audience.objects.create(organization=org, name="Readers", slug="readers")
    contact = Contact.objects.create(email="reader@example.com", verified_at=timezone.now())
    Subscription.objects.create(contact=contact, audience=audience, client=client, status="subscribed")
    return Campaign.objects.create(client=client, audience=audience, subject="Later", html_body="<h1>Later</h1>",
                                   scheduled_at=timezone.now() + timedelta(hours=1))


def test_schedule_acceptance_no_snapshot_no_enqueue_and_idempotent(campaign):
    with patch("mailing.services.campaigns.enqueue_campaign_email") as enqueue:
        result = queue_campaign(campaign)
        repeat = queue_campaign(campaign)
    campaign.refresh_from_db()
    assert campaign.status == "scheduled"
    assert result.scheduled and result.queued and result.batch_count == 0
    assert repeat.scheduled and not repeat.queued
    assert not campaign.recipients.exists()
    assert not CampaignDispatch.objects.exists()
    enqueue.assert_not_called()


def test_dispatch_not_early_then_due_once_with_send_time_snapshot(campaign, django_capture_on_commit_callbacks):
    queue_campaign(campaign)
    late_contact = Contact.objects.create(email="late@example.com", verified_at=timezone.now())
    Subscription.objects.create(contact=late_contact, audience=campaign.audience, client=campaign.client, status="subscribed")
    with patch("mailing.services.campaigns.enqueue_campaign_email") as enqueue:
        assert dispatch_due_campaigns(now=campaign.scheduled_at - timedelta(seconds=1)) == []
        with django_capture_on_commit_callbacks(execute=True):
            results = dispatch_due_campaigns(now=campaign.scheduled_at)
        assert len(results) == 1
        assert results[0].recipient_count == 2
        assert enqueue.call_count == 1
        assert dispatch_due_campaigns(now=campaign.scheduled_at + timedelta(minutes=1)) == []
        assert enqueue.call_count == 1
    campaign.refresh_from_db()
    assert campaign.status == "queued"
    assert campaign.scheduled_dispatch.enqueued_at is not None


def test_cancel_scheduled_prevents_dispatch(campaign):
    queue_campaign(campaign)
    assert cancel_campaign_instance(campaign)["cancelled"]
    with patch("mailing.services.campaigns.enqueue_campaign_email") as enqueue:
        assert dispatch_due_campaigns(now=campaign.scheduled_at + timedelta(hours=1)) == []
    enqueue.assert_not_called()
    assert not campaign.recipients.exists()


@pytest.mark.parametrize("status", ["scheduled", "queued"])
def test_stale_early_task_does_not_send_or_change_pending(campaign, status):
    campaign.status = status
    campaign.save()
    contact = Contact.objects.get(email="reader@example.com")
    recipient = CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=contact.email, status="pending")
    provider = Mock()
    with pytest.raises(RetryableCampaignSendError, match="not arrived"):
        send_campaign_batch({"campaign_id": campaign.id, "campaign_recipient_ids": [recipient.id]}, ses_client=provider)
    provider.send_email.assert_not_called()
    recipient.refresh_from_db()
    assert recipient.status == "pending"
    assert not recipient.tracking_token_hash


def test_outbox_recovers_interrupted_commit_callback_without_resnapshot(campaign, django_capture_on_commit_callbacks):
    queue_campaign(campaign)
    # No callback execution simulates a crash after the campaign transaction.
    dispatch_due_campaigns(now=campaign.scheduled_at)
    dispatch = CampaignDispatch.objects.get()
    assert dispatch.enqueued_at is None
    with patch("mailing.services.campaigns.enqueue_campaign_email") as enqueue:
        assert recover_campaign_dispatches() == 1
        assert recover_campaign_dispatches() == 0
    assert enqueue.call_count == 1
    assert campaign.recipients.count() == 1


def test_outbox_enqueue_error_retries_and_retains_intent(campaign):
    queue_campaign(campaign)
    dispatch_due_campaigns(now=campaign.scheduled_at)
    with patch("mailing.services.campaigns.enqueue_campaign_email", side_effect=RuntimeError("offline")):
        assert recover_campaign_dispatches() == 0
    dispatch = CampaignDispatch.objects.get()
    assert dispatch.enqueued_at is None
    assert "offline" in dispatch.last_error
    with patch("mailing.services.campaigns.enqueue_campaign_email") as enqueue:
        assert recover_campaign_dispatches() == 1
    enqueue.assert_called_once()
    dispatch.refresh_from_db()
    assert dispatch.last_error == ""


def test_due_dispatch_rollback_sends_nothing(campaign, django_capture_on_commit_callbacks):
    queue_campaign(campaign)
    with patch("mailing.services.campaigns.enqueue_campaign_email") as enqueue:
        with pytest.raises(ValueError), django_capture_on_commit_callbacks(execute=True), transaction.atomic():
            dispatch_due_campaigns(now=campaign.scheduled_at)
            raise ValueError("rollback")
    enqueue.assert_not_called()
    campaign.refresh_from_db()
    assert campaign.status == "scheduled"
    assert not campaign.recipients.exists()
    assert not CampaignDispatch.objects.exists()


def test_api_requires_timezone_and_accepts_offset(campaign):
    base = {"audience": campaign.audience.slug, "client": campaign.client.slug,
            "subject": "Later", "html_body": "Hello", "text_body": "Hi"}
    with pytest.raises(ApiValidationError) as exc:
        validate_campaign_payload(base | {"scheduled_at": "2026-12-01T15:00:00"}, campaign.client)
    assert exc.value.errors["scheduled_at"] == "timezone_required"
    result = validate_campaign_payload(base | {"scheduled_at": "2026-12-01T15:00:00+02:00"}, campaign.client)
    assert timezone.is_aware(result["scheduled_at"])
    assert result["scheduled_at"].utcoffset() == timedelta(hours=2)


def test_existing_scheduler_command_dispatches_due_campaigns(campaign):
    with patch("jobs.management.commands.run_relay_scheduler.dispatch_due_campaigns", return_value=[Mock()]) as due, \
         patch("jobs.management.commands.run_relay_scheduler.run_due_schedules", return_value=[]), \
         patch("jobs.management.commands.run_relay_scheduler.fail_expired_leases", return_value=0), \
         patch("jobs.management.commands.run_relay_scheduler.recover_unenqueued_jobs", return_value=0):
        output = StringIO()
        call_command("run_relay_scheduler", once=True, stdout=output)
    due.assert_called_once_with()
    assert "campaigns_dispatched=1" in output.getvalue()


def test_changed_scheduled_content_fails_visibly_without_repeated_dispatch(campaign):
    queue_campaign(campaign)
    Campaign.objects.filter(pk=campaign.pk).update(subject="")
    with patch("mailing.services.campaigns.enqueue_campaign_email") as enqueue:
        assert dispatch_due_campaigns(now=campaign.scheduled_at) == []
        assert dispatch_due_campaigns(now=campaign.scheduled_at) == []
    enqueue.assert_not_called()
    campaign.refresh_from_db()
    assert campaign.status == "failed"
    assert "subject" in campaign.metadata["scheduling_error"]
    assert not campaign.recipients.exists()


def test_cancel_after_due_commit_but_before_outbox_dispatch_stops_tasks(campaign):
    queue_campaign(campaign)
    dispatch_due_campaigns(now=campaign.scheduled_at)
    assert cancel_campaign_instance(campaign)["cancelled"]
    with patch("mailing.services.campaigns.enqueue_campaign_email") as enqueue:
        assert recover_campaign_dispatches() == 1
    enqueue.assert_not_called()
    assert not campaign.recipients.filter(status="pending").exists()


def test_api_bad_date_is_validation_error(campaign):
    with pytest.raises(ApiValidationError) as exc:
        validate_campaign_payload({"audience": campaign.audience.slug, "client": campaign.client.slug,
                                   "subject": "Later", "html_body": "Hello", "text_body": "Hi",
                                   "scheduled_at": "2026-19-01T15:00:00+02:00"}, campaign.client)
    assert exc.value.errors["scheduled_at"] == "invalid"



def test_due_delivery_duplicate_task_calls_provider_once(campaign, django_capture_on_commit_callbacks):
    queue_campaign(campaign)
    Campaign.objects.filter(pk=campaign.pk).update(scheduled_at=timezone.now() - timedelta(seconds=1))
    payloads = []
    with patch("mailing.services.campaigns.enqueue_campaign_email", side_effect=payloads.append), \
         django_capture_on_commit_callbacks(execute=True):
        assert len(dispatch_due_campaigns()) == 1
    provider = Mock()
    provider.send_email.return_value = {"MessageId": "scheduled-id"}
    assert send_campaign_batch(payloads[0], ses_client=provider).sent_count == 1
    assert send_campaign_batch(payloads[0], ses_client=provider).sent_count == 0
    provider.send_email.assert_called_once()
    assert campaign.recipients.get().ses_message_id == "scheduled-id"


def test_due_snapshot_rechecks_unsubscribe_and_completes_empty(campaign, django_capture_on_commit_callbacks):
    queue_campaign(campaign)
    Subscription.objects.filter(client=campaign.client).update(status="unsubscribed")
    with patch("mailing.services.campaigns.enqueue_campaign_email") as enqueue, \
         django_capture_on_commit_callbacks(execute=True):
        result = dispatch_due_campaigns(now=campaign.scheduled_at)[0]
    enqueue.assert_not_called()
    assert result.recipient_count == 0
    assert result.skipped_count == 1
    campaign.refresh_from_db()
    assert campaign.status == "sent"
    assert campaign.recipients.get().status == "skipped"



@pytest.mark.parametrize("changed_field", ["subject", "scheduled_at"])
def test_review_revision_rejects_changed_content_or_send_time_before_snapshot(campaign, changed_field):
    reviewed_revision = campaign.updated_at.isoformat()
    if changed_field == "subject":
        campaign.subject = "Changed after review"
    else:
        campaign.scheduled_at += timedelta(hours=2)
    campaign.save()
    with patch("mailing.services.campaigns.snapshot_campaign_recipients") as snapshot, \
         patch("mailing.services.campaigns.enqueue_campaign_email") as enqueue:
        with pytest.raises(CampaignNotReady, match="changed after review"):
            queue_campaign(campaign, expected_revision=reviewed_revision, reject_past_schedule=True)
    snapshot.assert_not_called()
    enqueue.assert_not_called()
    campaign.refresh_from_db()
    assert campaign.status == "draft"
    assert not CampaignDispatch.objects.exists()


@pytest.mark.parametrize("seconds_after", [0, 1])
def test_operator_expired_schedule_never_becomes_immediate_send(campaign, seconds_after):
    with patch("mailing.services.campaigns.snapshot_campaign_recipients") as snapshot, \
         patch("mailing.services.campaigns.enqueue_campaign_email") as enqueue:
        with pytest.raises(CampaignNotReady, match="Choose a future time"):
            queue_campaign(campaign, expected_revision=campaign.updated_at.isoformat(),
                           reject_past_schedule=True, now=campaign.scheduled_at + timedelta(seconds=seconds_after))
    snapshot.assert_not_called()
    enqueue.assert_not_called()
    campaign.refresh_from_db()
    assert campaign.status == "draft"
    assert not campaign.recipients.exists()


def test_matching_review_accepts_future_schedule(campaign):
    result = queue_campaign(campaign, expected_revision=campaign.updated_at.isoformat(), reject_past_schedule=True)
    assert result.scheduled
    campaign.refresh_from_db()
    assert campaign.status == "scheduled"
    assert not campaign.recipients.exists()


def test_default_api_past_schedule_keeps_immediate_queue_semantics(campaign, django_capture_on_commit_callbacks):
    with patch("mailing.services.campaigns.enqueue_campaign_email") as enqueue, \
         django_capture_on_commit_callbacks(execute=True):
        result = queue_campaign(campaign, now=campaign.scheduled_at + timedelta(seconds=1))
    assert result.queued and not result.scheduled
    enqueue.assert_called_once()
    campaign.refresh_from_db()
    assert campaign.status == "queued"
