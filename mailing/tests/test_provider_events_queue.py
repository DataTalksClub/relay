"""SES/inbound tasks must not wait behind default-queue send fan-out."""

import json

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.tasks import TaskResultStatus
from django.urls import reverse
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from mailing import tasks as mailing_tasks
from mailing.ingress import get_ingress_config
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
)
from mailing.services.ses_webhooks import SNS_MOCK_SIGNATURE
from mailing.sqs_worker import SqsWorker
from mailing.tests.test_ingress import FakeSqsClient, _message
from taskdeck.models import TaskRun, TaskRunStatus

pytestmark = pytest.mark.django_db


def test_provider_events_tasks_declare_dedicated_queue():
    assert mailing_tasks.process_ses_webhook_event.queue_name == "provider-events"
    assert mailing_tasks.process_inbound_email.queue_name == "provider-events"
    assert mailing_tasks.send_campaign_email_batch.queue_name == "default"
    assert mailing_tasks.send_transactional_email.queue_name == "default"
    assert mailing_tasks.send_transactional_email_batch.queue_name == "default"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("transport", ["http", "sqs"])
def test_ses_event_not_blocked_by_default_queue_backlog(client, settings, transport, admin_user):
    """Run the actual provider worker while 50 real campaign batches stay ready."""
    settings.TASKDECK_STATUS_TOKEN = "test-provider-lane-status"
    settings.SES_WEBHOOKS_SIGNATURE_MODE = "mock"
    settings.SQS_SES_WEBHOOKS_QUEUE_URL = "https://sqs.example/ses"
    organization = Organization.objects.create(name="Lane test", slug="lane-test")
    audience = Audience.objects.create(organization=organization, name="Lane test", slug="lane-test")
    app_client = Client.objects.create(organization=organization, name="Lane test", slug="lane-test")
    campaign = Campaign.objects.create(client=app_client, audience=audience, subject="Lane test")
    contact = Contact.objects.create(email="lane@example.com")
    recipient = CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email=contact.email,
        status=CampaignRecipientStatus.SENT,
        ses_message_id="lane-test-message",
        sent_at=timezone.now(),
    )
    for _ in range(50):
        mailing_tasks.send_campaign_email_batch.enqueue({
            "contract": "campaign-email",
            "version": 1,
            "campaign_id": campaign.pk,
            "recipient_ids": [recipient.pk],
        })

    notification = {
        "Type": "Notification",
        "MessageId": "lane-test-delivery",
        "Signature": SNS_MOCK_SIGNATURE,
        "Message": json.dumps({
            "eventType": "Delivery",
            "mail": {"messageId": recipient.ses_message_id},
        }),
    }
    if transport == "http":
        response = client.post(
            reverse("mailing:ses_webhook"),
            data=json.dumps(notification),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert response.json()["enqueued"] is True
    else:
        sqs = FakeSqsClient([_message("lane-test", notification)])
        result = SqsWorker(get_ingress_config("ses-webhooks"), client=sqs).run_once()
        assert (result.received, result.deleted, result.failed) == (1, 1, 0)

    queued = DBTaskResult.objects.get(task_path__endswith="process_ses_webhook_event")
    assert queued.queue_name == "provider-events"
    assert queued.status == TaskResultStatus.READY
    recipient.refresh_from_db()
    assert recipient.delivered_at is None
    assert not EmailEvent.objects.exists()
    client.force_login(admin_user)
    settings.WORKER_STATUS_SYSTEMD_ENABLED = False
    _assert_ses_status(client, TaskRunStatus.QUEUED)

    call_command("db_worker", "--queue-name", "provider-events", "--batch", "--interval", "0")

    queued.refresh_from_db()
    assert queued.status == TaskResultStatus.SUCCESSFUL
    recipient.refresh_from_db()
    assert recipient.delivered_at is not None
    assert recipient.status == CampaignRecipientStatus.SENT
    assert EmailEvent.objects.get(provider_event_id="lane-test-delivery").event_type == EmailEventType.DELIVERED
    assert DBTaskResult.objects.filter(queue_name="default", status=TaskResultStatus.READY).count() == 50
    assert not DBTaskResult.objects.filter(queue_name="default", started_at__isnull=False).exists()
    assert not DBTaskResult.objects.filter(queue_name="provider-events", status=TaskResultStatus.READY).exists()
    _assert_ses_status(client, TaskRunStatus.SUCCESS)


def _assert_ses_status(client, expected_status):
    cache.clear()
    workers_response = client.get("/api/workers/status")
    assert workers_response.status_code == 200
    assert any(worker["key"] == "ses-webhooks" for worker in workers_response.json()["workers"])
    response = client.get(
        reverse("mailing:taskdeck_status"),
        headers={"authorization": "Bearer test-provider-lane-status"},
    )
    assert response.status_code == 200
    runs = [run for run in response.json()["recent_runs"] if run["name"] == "process_ses_webhook_event"]
    assert len(runs) == 1
    assert runs[0]["status"] == expected_status
    assert TaskRun.objects.get(name="process_ses_webhook_event").status == expected_status
    cache.clear()
