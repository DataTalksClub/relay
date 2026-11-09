"""Background tasks, defined against the standard ``django.tasks`` interface.

These wrap the same service functions the SQS handlers in
``mailing/workers/handlers.py`` call, so behaviour is unchanged -- only the
transport moves. The handlers remain in place for the two queues AWS services
write to directly (see ``mailing/ingress.py``).

Nothing here imports a queue backend. The backend is chosen by ``TASKS`` in
settings.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, InterfaceError
from django.tasks import task
from django.utils import timezone

import taskdeck
from mailing.db_retry import deadlock_diag_lines, is_transient_db_error, jittered_backoff_seconds, run_with_db_retry
from mailing.queue_contracts import (
    validate_campaign_email_message,
    validate_email_event_message,
    validate_transactional_email_message,
)
from mailing.services.campaign_sender import RetryableCampaignSendError, send_campaign_batch
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


@task(queue_name="transactional", priority=50)
def send_transactional_email(payload, *, attempt=1):
    """Render and send one transactional message."""
    validate_transactional_email_message(payload)

    client_id = payload.get("client_id")
    if client_id:
        taskdeck.set_owner(client_id)
    message_id = payload.get("transactional_message_id")
    if message_id:
        taskdeck.set_entity("transactional_message", message_id)

    try:
        message = send_transactional_email_from_queue(payload)
    except Exception as exc:
        # django-tasks-db records FAILED tasks but does not retry them. Queue
        # a separate delayed attempt, retaining the original failure in the
        # task history. The service's persisted claim/terminal checks remain
        # the authority on whether SES may be called again.
        if attempt < settings.RELAY_TRANSACTIONAL_MAX_ATTEMPTS:
            delay = min(
                settings.RELAY_TRANSACTIONAL_RETRY_BASE_SECONDS * 2 ** (attempt - 1),
                settings.RELAY_TRANSACTIONAL_RETRY_MAX_SECONDS,
            )
            result = send_transactional_email.using(
                run_after=timezone.now() + timedelta(seconds=delay),
            ).enqueue(payload, attempt=attempt + 1)
            taskdeck.stamp(result, entity=("transactional_message", message_id), owner_id=client_id)
        else:
            from mailing.services.transactional_reliability import (  # noqa: PLC0415 - avoids enqueue cycle
                fail_exhausted_transactional_message,
            )

            fail_exhausted_transactional_message(payload, attempt, exc)
        raise
    return _summarise(message, lambda m: {"transactional_message_id": m.pk, "status": m.status})


@task(queue_name="campaign", priority=-50)
def send_campaign_email_batch(payload, *, attempt=1):
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

    try:
        result = send_campaign_batch(payload)
    except RetryableCampaignSendError as exc:
        return _requeue_throttled_campaign_batch(payload, attempt, exc)
    return _summarise(
        result,
        lambda r: {"sent": r.sent_count, "skipped": r.skipped_count, "failed": r.failed_count},
    )


def _requeue_throttled_campaign_batch(payload, attempt, exc):
    """Requeue the remaining PENDING recipients instead of failing the batch.

    SES throttling means the failed recipient (and everything after it) was
    never attempted, so failing the batch would misreport unsent mail. The
    remaining PENDING rows of this batch go out as one new task with run_after
    backoff and bounded attempts; exhaustion re-raises so the batch is FAILED
    loudly instead of retrying forever. A halted (paused/stopped) campaign is
    never requeued: resume re-enqueues what is still pending.
    """
    from mailing.models import Campaign, CampaignRecipient, CampaignRecipientStatus  # noqa: PLC0415
    from mailing.services.campaign_sender import _campaign_send_halted  # noqa: PLC0415

    campaign_id = payload.get("campaign_id")
    batch_recipient_ids = list(payload.get("campaign_recipient_ids") or [])
    max_attempts = max(1, int(getattr(settings, "RELAY_CAMPAIGN_THROTTLE_MAX_ATTEMPTS", 8) or 8))
    base_seconds = max(1, int(getattr(settings, "RELAY_CAMPAIGN_THROTTLE_BASE_SECONDS", 30) or 30))
    max_seconds = max(1, int(getattr(settings, "RELAY_CAMPAIGN_THROTTLE_MAX_SECONDS", 900) or 900))

    if campaign_id and _campaign_send_halted(campaign_id):
        logger.error(
            "campaign_send_throttled_but_halted campaign_id=%s batch_id=%s attempt=%d error=%s",
            campaign_id, payload.get("batch_id"), attempt, str(exc)[:200],
        )
        return {"deferred": False, "halted": True, "attempt": attempt}

    remaining_ids = []
    if campaign_id and batch_recipient_ids:
        remaining_ids = list(
            CampaignRecipient.objects.filter(
                campaign_id=campaign_id,
                pk__in=batch_recipient_ids,
                status=CampaignRecipientStatus.PENDING,
            )
            .order_by("id")
            .values_list("id", flat=True)
        )
    if campaign_id and not Campaign.objects.filter(pk=campaign_id).exists():
        logger.error(
            "campaign_send_throttle_exhausted campaign_id=%s batch_id=%s attempt=%d remaining=%d error=%s",
            campaign_id, payload.get("batch_id"), attempt, len(remaining_ids), str(exc)[:200],
        )
        raise exc
    if not remaining_ids:
        logger.error(
            "campaign_send_throttled_no_pending campaign_id=%s batch_id=%s attempt=%d error=%s",
            campaign_id, payload.get("batch_id"), attempt, str(exc)[:200],
        )
        return {"deferred": False, "remaining": 0, "attempt": attempt}
    if attempt >= max_attempts:
        logger.error(
            "campaign_send_throttle_exhausted campaign_id=%s batch_id=%s attempt=%d/%d remaining=%d error=%s",
            campaign_id, payload.get("batch_id"), attempt, max_attempts, len(remaining_ids), str(exc)[:200],
        )
        raise exc

    delay = min(base_seconds * 2 ** (attempt - 1), max_seconds)
    next_attempt = attempt + 1
    retry_payload = {
        **payload,
        "batch_id": f"{payload.get('batch_id')}-throttle-retry-{next_attempt}",
        "campaign_recipient_ids": remaining_ids,
        "idempotency_key": f"{payload.get('batch_id')}-throttle-retry-{next_attempt}",
    }
    validate_campaign_email_message(retry_payload)
    result = send_campaign_email_batch.using(
        run_after=timezone.now() + timedelta(seconds=delay),
    ).enqueue(retry_payload, attempt=next_attempt)
    taskdeck.stamp(result, entity=("campaign", campaign_id) if campaign_id else None)
    logger.error(
        "campaign_send_throttled_requeued campaign_id=%s batch_id=%s attempt=%d/%d remaining=%d retry_in_s=%d error=%s",
        campaign_id, payload.get("batch_id"), next_attempt, max_attempts, len(remaining_ids), delay, str(exc)[:200],
    )
    return {"deferred": True, "next_attempt": next_attempt, "retry_in_seconds": delay, "remaining": len(remaining_ids)}


@task(queue_name="transactional", priority=50)
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


def _run_provider_event(task_obj, work, payload, attempt):
    """Run one provider-events task body, surviving transient DB errors.

    A deadlock, serialization failure, refused/closed connection or DNS miss
    aborts only the current transaction, and the provider-event services are
    idempotent (SES events dedup on ``EmailEvent.provider_event_id``, inbound
    mail on ``InboundMessage.message_id``) and transactional, so running them
    again is safe. Immediate retries (``run_with_db_retry``) absorb a single
    aborted transaction; if the error outlasts them, a delayed attempt is
    enqueued with bounded exponential backoff instead of leaving the event
    permanently FAILED -- django-tasks-db never retries a FAILED task. The
    current task then returns a ``deferred`` summary rather than raising, so a
    handled transient error does not log a traceback. After
    ``RELAY_PROVIDER_EVENT_MAX_ATTEMPTS`` delayed attempts, or when the delayed
    attempt cannot be enqueued, the error is raised and the task is FAILED.
    """
    action = task_obj.name
    try:
        return run_with_db_retry(
            work,
            action=action,
            attempts=settings.RELAY_PROVIDER_EVENT_DB_RETRY_ATTEMPTS,
            base_delay_seconds=0.5,
            max_delay_seconds=4.0,
        )
    except (DatabaseError, InterfaceError) as exc:
        if not is_transient_db_error(exc) or attempt >= settings.RELAY_PROVIDER_EVENT_MAX_ATTEMPTS:
            raise
        # Jittered so two provider-event tasks that deadlocked on the same
        # rows do not wake up together and deadlock again.
        delay = jittered_backoff_seconds(
            settings.RELAY_PROVIDER_EVENT_RETRY_BASE_SECONDS,
            attempt,
            settings.RELAY_PROVIDER_EVENT_RETRY_MAX_SECONDS,
        )
        try:
            run_with_db_retry(
                lambda: task_obj.using(run_after=timezone.now() + timedelta(seconds=delay)).enqueue(
                    payload, attempt=attempt + 1
                ),
                action=f"{action} delayed retry enqueue",
                attempts=settings.RELAY_PROVIDER_EVENT_DB_RETRY_ATTEMPTS,
                base_delay_seconds=0.5,
                max_delay_seconds=4.0,
            )
        except (DatabaseError, InterfaceError):
            raise exc from None
        first_line = (str(exc).strip().splitlines() or [exc.__class__.__name__])[0]
        detail = " | ".join(deadlock_diag_lines(exc))
        logger.warning(
            "%s: retrying transient DB error as a delayed task (next attempt %d/%d in %ds): %s%s",
            action,
            attempt + 1,
            settings.RELAY_PROVIDER_EVENT_MAX_ATTEMPTS,
            delay,
            first_line,
            f"; {detail}" if detail else "",
        )
        return {"deferred": True, "next_attempt": attempt + 1, "retry_in_seconds": delay}


@task(queue_name="provider-events")
def process_ses_webhook_event(payload, *, attempt=1):
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

    return _run_provider_event(
        process_ses_webhook_event,
        lambda: _summarise_ses_result(process_ses_webhook(normalize_ses_webhook_worker_payload(payload))),
        payload,
        attempt,
    )


def _summarise_ses_result(result):
    if result is None:
        return None
    if hasattr(result, "action"):
        return {
            "operator_audit_id": getattr(result, "pk", getattr(result, "id", None)),
            "action": result.action,
        }
    return {
        "email_event_id": getattr(result, "pk", getattr(result, "id", None)),
        "event_type": getattr(result, "event_type", None),
    }


@task(queue_name="provider-events")
def process_inbound_email(payload, *, attempt=1):
    """Handle one inbound-mail object-storage notification."""
    return _run_provider_event(
        process_inbound_email,
        lambda: process_inbound_s3_notification(payload),
        payload,
        attempt,
    )


@task()
def process_email_event(payload):
    """Ingest one edge-generated tracking event.

    Body is intentionally a no-op, matching the existing handler:
    ``email_events`` is append-only and its ingest is not implemented yet. It
    exists so the queue has a task-side home when it is.
    """
    validate_email_event_message(payload)
    return None


@task(queue_name="tracking")
def process_tracking_event(payload, *, attempt=1):
    """Persist one recipient open/click accepted by the web (/t/o, /t/c)."""
    from mailing.services.tracking import apply_tracking_event  # noqa: PLC0415 - tracking enqueues these tasks

    return _run_provider_event(
        process_tracking_event,
        lambda: _summarise(apply_tracking_event(payload), lambda event: {"email_event_id": event.pk}),
        payload,
        attempt,
    )


@task(queue_name="tracking", priority=50)
def apply_deferred_unsubscribe(payload, *, attempt=1):
    """Apply an unsubscribe the web could not write within its lock timeout.

    Waits for the recipient lock instead of timing out, and retries transient DB
    errors, so the accepted unsubscribe is not lost; a final failure leaves a
    FAILED row that the queue monitor reports.
    """
    from datetime import datetime  # noqa: PLC0415

    from mailing.services.tracking import apply_unsubscribe_for_recipient  # noqa: PLC0415

    return _run_provider_event(
        apply_deferred_unsubscribe,
        lambda: _summarise(
            apply_unsubscribe_for_recipient(payload["recipient_id"], datetime.fromisoformat(payload["occurred_at"])),
            lambda recipient: {"campaign_recipient_id": recipient.pk},
        ),
        payload,
        attempt,
    )


@task(queue_name="tracking")
def refresh_campaign_engagement(campaign_id):
    from mailing.services.tracking import refresh_campaign_engagement_counts  # noqa: PLC0415

    return refresh_campaign_engagement_counts(campaign_id)
