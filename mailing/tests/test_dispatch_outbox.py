from datetime import timedelta
from unittest.mock import patch

import pytest
from django.conf import settings
from django.test import override_settings
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from mailing.campaign_ui_views import campaign_timing_context
from mailing.models import (
    Audience,
    Campaign,
    CampaignDispatch,
    CampaignDispatchBatch,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignTaskAlert,
    Client,
    Contact,
    Organization,
    Subscription,
)
from mailing.services import campaigns
from mailing.services.api import campaign_payload
from mailing.services.campaigns import queue_campaign, recover_campaign_dispatches
from mailing.services.task_leases import (
    CAMPAIGN_BATCH_TASK_PATH,
    STALE_EXCEPTION_PATH,
    fail_stale_campaign_tasks,
    record_task_heartbeat,
)

pytestmark = pytest.mark.django_db

BATCH_TASKS = {"task_path": CAMPAIGN_BATCH_TASK_PATH}


@pytest.fixture
def campaign():
    org = Organization.objects.create(name="Outbox", slug="outbox")
    client = Client.objects.create(organization=org, name="Newsletter", slug="newsletter")
    audience = Audience.objects.create(organization=org, name="Readers", slug="readers")
    for i in range(3):
        contact = Contact.objects.create(email=f"reader{i}@example.com", verified_at=timezone.now())
        Subscription.objects.create(contact=contact, audience=audience, client=client, status="subscribed")
    return Campaign.objects.create(client=client, audience=audience, subject="Now", html_body="<h1>Now</h1>")


def batch_tasks():
    return DBTaskResult.objects.filter(**BATCH_TASKS)


def test_crash_between_queued_commit_and_enqueue_is_recovered_once(campaign):
    # Callbacks are not run: simulates a crash right after the QUEUED commit.
    result = queue_campaign(campaign, batch_size=1)
    assert result.batch_count == 3
    campaign.refresh_from_db()
    assert campaign.status == "queued"
    assert batch_tasks().count() == 0
    dispatch = CampaignDispatch.objects.get(campaign=campaign)
    assert dispatch.enqueued_at is None
    assert campaign.recipients.filter(status="pending").count() == 3

    assert recover_campaign_dispatches() == 1
    assert batch_tasks().count() == 3
    covered = sorted(
        rid for t in batch_tasks() for rid in t.args_kwargs["args"][0]["campaign_recipient_ids"]
    )
    assert covered == sorted(campaign.recipients.values_list("id", flat=True))

    # Further runs (and a late on-commit callback) create nothing new.
    assert recover_campaign_dispatches() == 0
    assert batch_tasks().count() == 3
    assert CampaignDispatchBatch.objects.filter(campaign=campaign).count() == 3


def test_dispatch_partial_failure_rolls_back_and_retry_creates_each_batch_once(campaign):
    queue_campaign(campaign, batch_size=1)
    real = campaigns.enqueue_campaign_email
    calls = {"n": 0}

    def flaky(payload):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("queue offline")
        return real(payload)

    with patch.object(campaigns, "enqueue_campaign_email", flaky):
        assert recover_campaign_dispatches() == 0
    assert batch_tasks().count() == 0  # whole attempt rolled back, no half-dispatch
    assert "queue offline" in CampaignDispatch.objects.get().last_error
    assert recover_campaign_dispatches() == 1
    assert batch_tasks().count() == 3
    assert CampaignDispatchBatch.objects.count() == 3


def test_immediate_send_on_commit_fast_path_enqueues_once(campaign, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        queue_campaign(campaign, batch_size=2)
    assert batch_tasks().count() == 2
    assert recover_campaign_dispatches() == 0
    assert batch_tasks().count() == 2


def make_running_batch(campaign, recipients, *, started_ago):
    payload = {
        "campaign_id": campaign.id,
        "batch_id": f"campaign-{campaign.id}-batch-0001",
        "campaign_recipient_ids": [r.id for r in recipients],
    }
    return DBTaskResult.objects.create(
        args_kwargs={"args": [payload], "kwargs": {}}, task_path=CAMPAIGN_BATCH_TASK_PATH,
        queue_name="default", backend_name="default", status="RUNNING",
        started_at=timezone.now() - started_ago, worker_ids=["dead-worker"],
        run_after=timezone.now(), exception_class_path="", traceback="",
    )


def recipients_for(campaign):
    return [
        CampaignRecipient.objects.create(campaign=campaign, contact=c, email=c.email)
        for c in Contact.objects.all()
    ]


@override_settings(RELAY_TASK_LEASE_SECONDS=300)
def test_dead_worker_task_is_failed_loudly_with_campaign_alert(campaign, caplog):
    campaign.status = "sending"
    campaign.save()
    task = make_running_batch(campaign, recipients_for(campaign), started_ago=timedelta(minutes=6))
    alive = make_running_batch(campaign, [], started_ago=timedelta(minutes=30))
    record_task_heartbeat(alive.id, "live-worker")
    young = make_running_batch(campaign, [], started_ago=timedelta(minutes=1))

    with caplog.at_level("ERROR"):
        failed = fail_stale_campaign_tasks()

    assert failed == [task.pk]
    task.refresh_from_db()
    alive.refresh_from_db()
    young.refresh_from_db()
    assert task.status == "FAILED"
    assert task.exception_class_path == STALE_EXCEPTION_PATH
    assert task.traceback.startswith("FAILED_STALE")
    assert (alive.status, young.status) == ("RUNNING", "RUNNING")
    assert "STALE CAMPAIGN TASK" in caplog.text
    alert = CampaignTaskAlert.objects.get(campaign=campaign)
    assert "FAILED_STALE" in alert.message
    assert campaign_payload(campaign)["dispatch_alerts"][0]["task_id"] == str(task.pk)
    # Idempotent: the same task is not reported twice.
    assert fail_stale_campaign_tasks() == []
    assert CampaignTaskAlert.objects.filter(campaign=campaign).count() == 1


def test_stale_alert_shown_on_campaign_page(campaign, admin_client):
    campaign.status = "sending"
    campaign.save()
    make_running_batch(campaign, [], started_ago=timedelta(hours=1))
    fail_stale_campaign_tasks()
    assert "FAILED_STALE" in campaign_timing_context(campaign)["dispatch_alerts"][0]["message"]


def test_auto_reenqueue_is_off_by_default_and_only_reports(campaign):
    assert settings.RELAY_STALE_BATCH_AUTO_REENQUEUE is False
    make_running_batch(campaign, recipients_for(campaign), started_ago=timedelta(hours=1))
    with patch("mailing.services.task_leases.enqueue_campaign_email") as enqueue:
        assert len(fail_stale_campaign_tasks()) == 1
    enqueue.assert_not_called()


def test_auto_reenqueue_when_on_only_pending_recipients(campaign):
    recipients = recipients_for(campaign)
    CampaignRecipient.objects.filter(pk=recipients[0].pk).update(status=CampaignRecipientStatus.SENT)
    CampaignRecipient.objects.filter(pk=recipients[1].pk).update(status=CampaignRecipientStatus.FAILED)
    make_running_batch(campaign, recipients, started_ago=timedelta(hours=1))
    with override_settings(RELAY_STALE_BATCH_AUTO_REENQUEUE=True):
        with patch("mailing.services.task_leases.enqueue_campaign_email") as enqueue:
            assert len(fail_stale_campaign_tasks()) == 1
            assert fail_stale_campaign_tasks() == []
    enqueue.assert_called_once()
    retry = enqueue.call_args.args[0]
    assert retry["campaign_recipient_ids"] == [recipients[2].pk]
    assert retry["batch_id"].startswith(f"campaign-{campaign.id}-batch-0001-retry-")
