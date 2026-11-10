"""Worker heartbeat and stuck-task detection for campaign batch tasks.

``django_tasks_db`` marks a task RUNNING when a worker claims it and never
looks again: a worker that is killed (deploy, OOM, host loss) leaves the row
RUNNING forever and its campaign looks like it is still sending. The worker
beats ``TaskHeartbeat`` while a task runs; the scheduler fails any campaign
batch whose last beat (or start) is older than the lease.

``DBTaskResult.status`` is a 10-character column owned by the library, so the
"FAILED_STALE" state is recorded as FAILED with ``exception_class_path`` set to
``STALE_EXCEPTION_PATH`` and a traceback that starts with ``FAILED_STALE``.
"""

import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.models import DateTimeField, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from mailing.db_retry import record_db_heartbeat
from mailing.enqueue import enqueue_campaign_email
from mailing.models import CampaignRecipient, CampaignRecipientStatus, CampaignTaskAlert, TaskHeartbeat

logger = logging.getLogger(__name__)

STALE_EXCEPTION_PATH = "relay.stale_task.FAILED_STALE"
CAMPAIGN_BATCH_TASK_PATH = "mailing.tasks.send_campaign_email_batch"
MAX_ALERT_LENGTH = 2000


def record_task_heartbeat(task_id, worker_id=""):
    TaskHeartbeat.objects.update_or_create(
        task_id=task_id, defaults={"worker_id": str(worker_id)[:200], "beat_at": timezone.now()}
    )


class HeartbeatThread:
    """Beats for one running task from a background thread (own DB connection)."""

    def __init__(self, task_id, worker_id, interval=None):
        self.task_id = task_id
        self.worker_id = worker_id
        self.interval = interval or settings.RELAY_TASK_HEARTBEAT_SECONDS
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"heartbeat-{task_id}", daemon=True)

    def _beat(self):
        try:
            record_task_heartbeat(self.task_id, self.worker_id)
            record_db_heartbeat()
        except Exception:
            # Not swallowed: a missed beat is logged at ERROR; enough of them
            # and the task is declared stale, which is also loud.
            logger.exception("task heartbeat failed task=%s worker=%s", self.task_id, self.worker_id)

    def _run(self):
        try:
            self._beat()
            while not self._stop.wait(self.interval):
                self._beat()
        finally:
            connection.close()

    def __enter__(self):
        if connection.vendor == "sqlite":
            # Dev/test only: SQLite cannot take a second writer connection from
            # a thread (table locked). Production is Postgres.
            self._thread = None
            return self
        self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._thread is None:
            return False
        self._stop.set()
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            logger.error("task heartbeat thread did not stop within 10s task=%s", self.task_id)
        try:
            TaskHeartbeat.objects.filter(task_id=self.task_id).delete()
        except Exception:
            logger.exception("could not remove task heartbeat task=%s", self.task_id)
        return False


def find_stale_campaign_tasks(*, now=None, lease_seconds=None, limit=100):
    now = now or timezone.now()
    lease = timedelta(seconds=lease_seconds or settings.RELAY_TASK_LEASE_SECONDS)
    last_beat = TaskHeartbeat.objects.filter(task_id=OuterRef("pk")).values("beat_at")[:1]
    return list(
        DBTaskResult.objects.filter(status="RUNNING", task_path=CAMPAIGN_BATCH_TASK_PATH)
        .annotate(last_seen=Coalesce(Subquery(last_beat, output_field=DateTimeField()), "started_at"))
        .filter(last_seen__lt=now - lease)
        .order_by("started_at")[:limit]
    )


def fail_stale_campaign_tasks(*, now=None, lease_seconds=None, auto_reenqueue=None, limit=100):
    """Mark stale RUNNING campaign batches FAILED_STALE; returns the failed task ids."""
    now = now or timezone.now()
    if auto_reenqueue is None:
        auto_reenqueue = settings.RELAY_STALE_BATCH_AUTO_REENQUEUE
    failed = []
    for task in find_stale_campaign_tasks(now=now, lease_seconds=lease_seconds, limit=limit):
        payload = (task.args_kwargs or {}).get("args", [{}])[0] or {}
        campaign_id = payload.get("campaign_id")
        message = (
            f"FAILED_STALE: batch task {task.id} ({payload.get('batch_id', '?')}) was RUNNING since "
            f"{task.started_at:%Y-%m-%d %H:%M:%S} UTC with no worker heartbeat for over "
            f"{lease_seconds or settings.RELAY_TASK_LEASE_SECONDS}s (worker {task.worker_ids[-1:] or '?'}). "
            "The worker most likely died; recipients of this batch may be unsent or in an unknown state."
        )
        with transaction.atomic():
            marked = DBTaskResult.objects.filter(pk=task.pk, status="RUNNING").update(
                status="FAILED", finished_at=now, exception_class_path=STALE_EXCEPTION_PATH, traceback=message,
            )
            if not marked:
                continue
            if campaign_id:
                CampaignTaskAlert.objects.update_or_create(
                    task_id=task.pk, defaults={"campaign_id": campaign_id, "message": message[:MAX_ALERT_LENGTH]},
                )
            TaskHeartbeat.objects.filter(task_id=task.pk).delete()
        failed.append(task.pk)
        logger.error("STALE CAMPAIGN TASK campaign=%s %s", campaign_id, message)
        if campaign_id and auto_reenqueue:
            reenqueue_pending_recipients(payload, stale_task_id=task.pk)
        elif campaign_id:
            logger.error(
                "stale batch %s NOT re-enqueued (RELAY_STALE_BATCH_AUTO_REENQUEUE is off until WP1 is "
                "deployed); inspect the campaign's PENDING recipients before resending", task.pk,
            )
    return failed


def reenqueue_pending_recipients(payload, *, stale_task_id):
    """Re-enqueue only the batch's recipients that are still PENDING.

    Runs once per stale task: the caller only gets here after winning the
    RUNNING -> FAILED transition for that task.
    """
    pending = list(
        CampaignRecipient.objects.filter(
            campaign_id=payload["campaign_id"],
            pk__in=payload.get("campaign_recipient_ids", []),
            status=CampaignRecipientStatus.PENDING,
        ).order_by("id").values_list("id", flat=True)
    )
    if not pending:
        logger.warning("stale batch %s has no PENDING recipients; nothing to re-enqueue", stale_task_id)
        return None
    retry_id = f"{payload['batch_id']}-retry-{str(stale_task_id)[:8]}"
    retry = {**payload, "batch_id": retry_id, "idempotency_key": retry_id, "campaign_recipient_ids": pending}
    enqueue_campaign_email(retry)
    logger.warning("stale batch %s: re-enqueued %d PENDING recipients as %s", stale_task_id, len(pending), retry_id)
    return retry


def campaign_dispatch_alerts(campaign):
    return [
        {"task_id": str(a.task_id), "message": a.message, "created_at": a.created_at.isoformat()}
        for a in campaign.task_alerts.all()[:10]
    ]
