"""Background tasks, defined against the standard ``django.tasks`` interface.

These wrap the same service functions the SQS/Lambda handlers in
``mailing/workers/handlers.py`` call, so behaviour is unchanged — only the
transport moves. The handlers remain in place for the two queues that AWS
services write to directly (see ``mailing/ingress.py``).

Nothing here imports a queue backend. The backend is chosen by ``TASKS`` in
settings.
"""

import logging

import taskdeck
from django.tasks import task

logger = logging.getLogger(__name__)


@task()
def send_transactional_email(payload):
    """Render and send one transactional message."""
    from mailing.queue_contracts import validate_transactional_email_message
    from mailing.services.transactional_sender import send_transactional_email_from_queue

    validate_transactional_email_message(payload)

    client_id = payload.get("client_id")
    if client_id:
        taskdeck.set_owner(client_id)
    message_id = payload.get("transactional_message_id")
    if message_id:
        taskdeck.set_entity("transactional_message", message_id)

    return send_transactional_email_from_queue(payload)


@task()
def send_campaign_email_batch(payload):
    """Send one batch of a campaign's recipients.

    Progress for the campaign as a whole is not derived from these tasks. The
    campaign's own ``CampaignRecipient`` rows already carry per-recipient
    status, which is both more accurate and survives a task being retried, so
    the campaign view reads those instead. What the task layer contributes here
    is the correlation id linking every batch of one send together.
    """
    from mailing.queue_contracts import validate_campaign_email_message
    from mailing.services.campaign_sender import send_campaign_batch

    validate_campaign_email_message(payload)

    campaign_id = payload.get("campaign_id")
    if campaign_id:
        taskdeck.set_entity("campaign", campaign_id)
    recipient_ids = payload.get("recipient_ids") or []
    if recipient_ids:
        taskdeck.set_total(len(recipient_ids), message=f"{len(recipient_ids)} recipients")

    return send_campaign_batch(payload)


@task()
def process_ses_webhook_event(payload):
    """Apply one SES delivery/bounce/complaint notification."""
    from mailing.services.ses_webhooks import (
        normalize_ses_webhook_worker_payload,
        process_ses_webhook,
    )

    return process_ses_webhook(normalize_ses_webhook_worker_payload(payload))


@task()
def process_inbound_email(payload):
    """Handle one inbound-mail object-storage notification."""
    from mailing.services.inbound_email import process_inbound_s3_notification

    return process_inbound_s3_notification(payload)


@task()
def process_email_event(payload):
    """Ingest one edge-generated tracking event.

    Kept as a no-op body to match the existing handler: ``email_events`` is
    append-only and its ingest is not implemented yet. Present so the queue has
    a task-side home when it is.
    """
    from mailing.queue_contracts import validate_email_event_message

    validate_email_event_message(payload)
    return None
