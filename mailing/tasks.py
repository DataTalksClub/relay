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


def _summarise(value, to_dict):
    """Reduce a service return value to something a task backend can store.

    The services return model instances and dataclasses because their other
    caller -- the SQS handlers in ``mailing/workers`` -- uses them. A task
    backend stores the return value instead, and ``django-tasks-db`` stores it
    in a JSONField, so handing one of those objects back marks the task failed
    *after* its work has already been committed: the email is sent and the row
    says it was not.

    Summarising here keeps that failure mode out of every task, and keeps the
    services free to return whatever suits their other caller.
    """
    return None if value is None else to_dict(value)


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

    message = send_transactional_email_from_queue(payload)
    return _summarise(message, lambda m: {"transactional_message_id": m.pk, "status": m.status})


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

    result = send_campaign_batch(payload)
    return _summarise(
        result,
        lambda r: {"sent": r.sent_count, "skipped": r.skipped_count, "failed": r.failed_count},
    )


@task()
def send_transactional_email_batch(message_ids, list_key="", template_key="", client_id=None):
    """Fan out one bulk transactional send -- a scoring run, a reminder sweep.

    The recipient-list endpoints used to enqueue one send per member straight
    from the request. Those are siblings with no parent, so a run that scores
    300 students produced 300 unrelated rows and no way to ask how far along it
    was, or whether it had finished at all.

    Enqueuing the children *from inside this task* is what fixes that: taskdeck
    links a task to whichever run enqueued it, so they become this run's
    children, and ``set_total`` here gives the denominator. The numerator is
    derived from finished children rather than counted up as they go, so a
    child that dies without reporting cannot corrupt it.

    Takes message ids rather than whole payloads. The rows are already
    committed by the time this runs, and a few thousand rendered payloads would
    otherwise be copied into the task's arguments.
    """
    from mailing.enqueue import enqueue_transactional_email  # noqa: PLC0415 - breaks an import cycle
    from mailing.models import TransactionalMessage  # noqa: PLC0415 - keeps tasks import-light
    from mailing.services.transactional import (  # noqa: PLC0415 - breaks an import cycle
        build_transactional_queue_payload,
    )

    if client_id:
        taskdeck.set_owner(client_id)
    if list_key:
        taskdeck.set_entity("recipient_list", list_key)

    message_ids = list(message_ids or [])
    taskdeck.set_total(len(message_ids), message=_batch_message(template_key, len(message_ids)))

    messages = TransactionalMessage.objects.select_related("client", "contact", "template").in_bulk(message_ids)
    enqueued = 0
    for message_id in message_ids:
        message = messages.get(message_id)
        if message is None:
            # Deleted between commit and pickup. Skipping keeps the rest of the
            # batch moving; the total stays honest because the child never
            # appears and the parent's progress reflects what actually ran.
            logger.warning("taskdeck: batch %s skipped missing message %s", list_key, message_id)
            continue
        enqueue_transactional_email(build_transactional_queue_payload(message))
        enqueued += 1

    return {"enqueued": enqueued, "requested": len(message_ids), "template_key": template_key}


def _batch_message(template_key, count):
    recipients = f"{count} recipient{'' if count == 1 else 's'}"
    return f"{template_key}: {recipients}" if template_key else recipients


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

    event = process_ses_webhook(normalize_ses_webhook_worker_payload(payload))
    return _summarise(event, lambda e: {"email_event_id": e.pk, "event_type": e.event_type})


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
