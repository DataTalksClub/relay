"""Durable, explicitly confirmed test sends with no automatic replay."""
import uuid

from django.db import transaction
from django.tasks import task
from django.tasks.exceptions import InvalidTask
from django.utils import timezone

import taskdeck
from mailing.models import Campaign
from mailing.services.api import validate_test_recipient_emails
from mailing.services.campaign_sender import send_campaign_test_message
from taskdeck.models import TaskRun


@transaction.atomic
def enqueue_campaign_test(campaign, emails):
    emails = validate_test_recipient_emails(emails)
    snapshot = {field: getattr(campaign, field) for field in (
        "id", "client_id", "subject", "preview_text", "html_body", "text_body",
    )}
    confirmed_id = uuid.uuid4()
    # Deferred execution prevents a misconfigured immediate backend from ever
    # making a provider call on the request thread. Results must be durable.
    queued_task = send_campaign_test.using(run_after=timezone.now())
    if not queued_task.get_backend().supports_get_result:
        raise InvalidTask("Campaign tests need a deferred backend with durable results.")
    with taskdeck.bind_correlation(confirmed_id):
        result = queued_task.enqueue(snapshot, emails, str(confirmed_id))
    taskdeck.stamp(result, entity=("campaign", campaign.pk), owner_id=campaign.client_id,
                   message=f"Test queued for {len(emails)} explicit addresses. No provider acceptance confirmed yet.")
    return result


@task()
def send_campaign_test(snapshot, emails, confirmed_id):
    """Stop on any provider error; a persisted claim prevents replay on re-entry.

    A process crash can leave an uncertain provider outcome. Re-entering this
    confirmed task never retries that email or resumes the remaining addresses.
    A new test requires a new explicit operator confirmation.
    """
    emails = validate_test_recipient_emails(emails)
    with transaction.atomic():
        run = TaskRun.objects.select_for_update().get(correlation_id=confirmed_id, name="send_campaign_test")
        if run.progress_current is not None:
            return {"status": "already_attempted", "accepted_count": run.progress_current,
                    "message": "This confirmed test was already attempted. No messages were retried."}
        run.progress_current = 0
        run.progress_total = len(emails)
        run.message = "Test started. Provider outcomes may be uncertain if this worker stops. No automatic retry."
        run.save(update_fields=["progress_current", "progress_total", "message"])
    campaign = Campaign(**snapshot)
    accepted = []
    for index, email in enumerate(emails):
        try:
            message_id = send_campaign_test_message(campaign, email)
        except Exception:
            outcome = {
                "status": "partial" if accepted else "unconfirmed", "accepted_count": len(accepted),
                "accepted": accepted, "unconfirmed_email": email, "not_attempted": emails[index + 1:],
                "message": "The provider outcome for the stopped address is unconfirmed. No automatic retry.",
            }
            TaskRun.objects.filter(pk=run.pk).update(
                message=f"Test stopped: {len(accepted)} provider accepted; 1 unconfirmed; "
                        f"{len(emails) - index - 1} not attempted. No automatic retry.")
            return outcome
        accepted.append({"email": email, "message_id": message_id})
        TaskRun.objects.filter(pk=run.pk).update(
            progress_current=len(accepted), heartbeat_at=timezone.now(),
            message=f"Test: {len(accepted)}/{len(emails)} accepted by provider. Acceptance does not confirm delivery.")
    return {"status": "provider_accepted", "accepted_count": len(accepted), "accepted": accepted,
            "not_attempted": [], "message": "Provider acceptance does not confirm delivery. Check the test inboxes."}
