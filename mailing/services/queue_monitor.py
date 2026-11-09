"""Alert when a task lane stops draining: oldest runnable READY task too old.

A stopped or starved worker otherwise shows up only as missing opens/bounces
hours later. Checked from the scheduler loop; one alert per queue per hour.
FAILED deferred unsubscribes are reported too: each one is a recipient's
unsubscribe that has not been applied yet.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Min
from django.utils import timezone

from mailing.services.transactional_monitor import send_monitor_alert

logger = logging.getLogger(__name__)

ALERT_COOLDOWN = timedelta(hours=1)
CHECK_INTERVAL = timedelta(seconds=60)
DEFERRED_UNSUBSCRIBE_TASK = "mailing.tasks.apply_deferred_unsubscribe"
_last_alert_at = {}
_last_check = None


def queue_age_alerts(now):
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415

    alerts = []
    for queue_name, max_age_seconds in settings.RELAY_QUEUE_AGE_ALERT_SECONDS.items():
        if max_age_seconds <= 0:
            continue
        stats = (
            DBTaskResult.objects.ready()
            .filter(queue_name=queue_name)
            .aggregate(oldest=Min("enqueued_at"), ready=Count("id"))
        )
        if stats["oldest"] is None:
            continue
        age = (now - stats["oldest"]).total_seconds()
        if age >= max_age_seconds:
            alerts.append(
                {
                    "kind": "queue_age",
                    "queue": queue_name,
                    "oldest_ready_age_seconds": int(age),
                    "ready": stats["ready"],
                    "threshold_seconds": max_age_seconds,
                }
            )
    failed_unsubscribes = DBTaskResult.objects.filter(task_path=DEFERRED_UNSUBSCRIBE_TASK, status="FAILED").count()
    if failed_unsubscribes:
        alerts.append({"kind": "deferred_unsubscribe_failed", "queue": "tracking", "failed": failed_unsubscribes})
    return alerts


def check_queue_age(*, now=None, force=False):
    global _last_check
    now = now or timezone.now()
    if not force and _last_check and now - _last_check < CHECK_INTERVAL:
        return []
    _last_check = now
    sent = []
    try:
        for payload in queue_age_alerts(now):
            key = (payload["kind"], payload["queue"])
            logger.error("relay_queue_alert %s", payload)
            last = _last_alert_at.get(key)
            if last and now - last < ALERT_COOLDOWN:
                continue
            try:
                send_monitor_alert(payload)
            except Exception as exc:  # noqa: BLE001 - monitoring must not stop the scheduler
                logger.error("relay_queue_alert_delivery_failed kind=%s error_type=%s", key, type(exc).__name__)
            else:
                _last_alert_at[key] = now
                sent.append(payload)
    except Exception as exc:  # noqa: BLE001
        logger.error("relay_queue_monitor_failed error_type=%s", type(exc).__name__)
    return sent
