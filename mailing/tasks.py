"""Background tasks, defined against the standard ``django.tasks`` interface.

These wrap the same service functions the SQS handlers in
``mailing/workers/handlers.py`` call, so behaviour is unchanged -- only the
transport moves. The handlers remain in place for the two queues AWS services
write to directly (see ``mailing/ingress.py``).

Nothing here imports a queue backend. The backend is chosen by ``TASKS`` in
settings.
"""

import logging

import taskdeck
from django.tasks import task

from mailing.queue_contracts import (
    validate_campaign_email_message,
    validate_email_event_message,
    validate_transactional_email_message,
)
from mailing.services.campaign_sender import send_campaign_batch
from mailing.services.inbound_email import process_inbound_s3_notification
from mailing.services.transactional_sender import send_transactional_email_from_queue

logger = logging.getLogger(__name__)


@task()
def send_transactional_email(payload):
    """Render and send one transactional message."""
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
    status, which is both more accurate and survives a batch being retried, so
    the campaign view reads those instead. What the task layer contributes is
    the correlation id linking every batch of one send together.
    """
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
    """Apply one SES delivery/bounce/complaint notification.

    Imported inside the body deliberately: ``services.ses_webhooks`` imports
    ``mailing.enqueue``, which imports this module, so a module-level import
    here would be circular. The other services have no such cycle and are
    imported normally above.
    """
    from mailing.services.ses_webhooks import (  # noqa: PLC0415 - breaks an import cycle
        normalize_ses_webhook_worker_payload,
        process_ses_webhook,
    )

    return process_ses_webhook(normalize_ses_webhook_worker_payload(payload))


@task()
def process_inbound_email(payload):
    """Handle one inbound-mail object-storage notification."""
    return process_inbound_s3_notification(payload)


@task()
def process_email_event(payload):
    """Ingest one edge-generated tracking event.

    Body is intentionally a no-op, matching the existing handler:
    ``email_events`` is append-only and its ingest is not implemented yet. It
    exists so the queue has a task-side home when it is.
    """
    validate_email_event_message(payload)
    return None
