"""F1: SES/inbound tasks must not wait behind default-queue send fan-out."""

import pytest
from django.tasks import task
from django_tasks.base import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from mailing import tasks as mailing_tasks

pytestmark = pytest.mark.django_db


@task(queue_name="default")
def _slow_default_placeholder():
    """Stand-in for a campaign/transactional send occupying the default lane."""
    return "default-done"


def test_provider_events_tasks_declare_dedicated_queue():
    assert mailing_tasks.process_ses_webhook_event.queue_name == "provider-events"
    assert mailing_tasks.process_inbound_email.queue_name == "provider-events"
    assert mailing_tasks.send_campaign_email_batch.queue_name == "default"
    assert mailing_tasks.send_transactional_email.queue_name == "default"


def test_ses_event_not_blocked_by_default_queue_backlog(settings):
    """With 50 default-queue tasks ahead, a provider-events enqueue is still
    visible on its own lane (picked without draining the default backlog).
    """
    for _ in range(50):
        _slow_default_placeholder.enqueue()

    mailing_tasks.process_ses_webhook_event.enqueue(
        {
            "contract": "ses-webhooks",
            "version": 1,
            "notification_type": "Delivery",
            "message_id": "f1-isolation",
        }
    )

    default_pending = DBTaskResult.objects.filter(
        queue_name="default", status=TaskResultStatus.READY
    ).count()
    provider_pending = list(
        DBTaskResult.objects.filter(
            queue_name="provider-events", status=TaskResultStatus.READY
        )
    )

    assert default_pending >= 50
    assert len(provider_pending) == 1
    assert provider_pending[0].task_path.endswith("process_ses_webhook_event")
    # Ordering proof: provider-events row is not behind the default backlog
    # in its own queue (it is the only ready row there).
    assert (
        DBTaskResult.objects.filter(queue_name="provider-events").count() == 1
    )
