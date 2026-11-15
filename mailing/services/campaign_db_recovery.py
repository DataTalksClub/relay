"""Durable recovery of future campaign batches after a database outage.

The failed task is the outbox: no retry enqueue has to succeed while Postgres
is down. The scheduler revisits it after recovery, atomically inserting a
delayed successor and saving its receipt. Only batches opted in at enqueue
are eligible; deploying this code never restarts historical live campaigns.
"""

import logging
from datetime import timedelta

from django.db import OperationalError, transaction
from django.db.models import Q
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from mailing.db_retry import TRANSIENT_DB_ERROR_SNIPPETS, is_transient_db_error, jittered_backoff_seconds
from mailing.models import Campaign, CampaignRecipient, CampaignRecipientStatus, CampaignStatus
from mailing.services.task_leases import CAMPAIGN_BATCH_TASK_PATH, STALE_EXCEPTION_PATH

logger = logging.getLogger(__name__)


def recover_campaign_db_batches(*, limit=100):
    """Recover each eligible failure once, including failures to record a result.

    A lost result write leaves RUNNING; the existing heartbeat lease first
    marks it FAILED_STALE. Only PENDING rows are eligible, never SENDING or
    UNKNOWN (an SES call may already have happened). The sender still checks
    halt/cancel and claims recipients before any subsequent SES call.
    """
    errors = Q(exception_class_path=STALE_EXCEPTION_PATH)
    for snippet in TRANSIENT_DB_ERROR_SNIPPETS:
        errors |= Q(traceback__icontains=snippet)
    candidates = DBTaskResult.objects.filter(
        errors,
        status="FAILED", task_path=CAMPAIGN_BATCH_TASK_PATH,
        args_kwargs__args__0__db_recovery=1, return_value__isnull=True,
    ).order_by("finished_at", "id").values_list("pk", flat=True)[:limit]
    recovered = []
    for task_id in list(candidates):
        with transaction.atomic():
            original = DBTaskResult.objects.select_for_update().get(pk=task_id)
            if original.status != "FAILED" or original.return_value is not None:
                continue
            stale = original.exception_class_path == STALE_EXCEPTION_PATH
            db_error = original.exception_class_path.rsplit(".", 1)[-1] in {
                "OperationalError", "InterfaceError", "DatabaseError",
            }
            if not stale and not (db_error and is_transient_db_error(OperationalError(original.traceback))):
                continue
            receipt = _recover_one(original)
            original.return_value = receipt
            original.save(update_fields=["return_value"])
            if receipt.get("db_retry_task_id"):
                recovered.append(receipt["db_retry_task_id"])
    return recovered


def _recover_one(original):
    from mailing.tasks import send_campaign_email_batch  # noqa: PLC0415 - task/service import cycle

    payload = original.args_kwargs["args"][0]
    campaign_id = payload["campaign_id"]
    if not Campaign.objects.filter(
        pk=campaign_id, status__in=[CampaignStatus.QUEUED, CampaignStatus.SENDING], send_halted_at__isnull=True,
    ).exists():
        return {"db_retry_skipped": "campaign_not_sendable"}
    pending = list(CampaignRecipient.objects.filter(
        campaign_id=campaign_id, pk__in=payload["campaign_recipient_ids"], status=CampaignRecipientStatus.PENDING,
    ).order_by("id").values_list("id", flat=True))
    if not pending:
        return {"db_retry_skipped": "no_pending_recipients"}
    # Retry outages indefinitely with a capped delay; an outage lasting longer
    # than an attempt budget must not permanently strand a sending campaign.
    attempt = int(payload.get("db_recovery_attempt", 0)) + 1
    delay = jittered_backoff_seconds(30, min(attempt, 6), 900)
    retry_id = f"db-retry-{original.pk}"
    retry_payload = {
        **payload, "batch_id": retry_id, "idempotency_key": retry_id,
        "campaign_recipient_ids": pending, "db_recovery_attempt": attempt,
    }
    result = send_campaign_email_batch.using(
        backend=original.backend_name, run_after=timezone.now() + timedelta(seconds=delay),
    ).enqueue(retry_payload, **original.args_kwargs.get("kwargs", {}))
    logger.warning(
        "campaign_db_batch_recovered campaign_id=%s failed_task=%s successor=%s pending=%d delay=%.1f",
        campaign_id, original.pk, result.id, len(pending), delay,
    )
    return {"db_retry_task_id": str(result.id), "remaining": len(pending), "retry_in_seconds": delay}
