"""Produce the status contract payload from Relay's TaskRun projection.

See docs/contract.md. Everything here is derived from taskdeck's own tables, so
a project gets a working endpoint without writing collector code. Projects
override pieces they can answer better — notably ``entity`` resolution, which
needs their domain models.
"""

import datetime

from django.db.models import Count, Q
from django.utils import timezone

from taskdeck.models import TaskRun, TaskRunStatus

CONTRACT_VERSION = 1

# A worker that has not checked in for this long reads as unhealthy. Chosen to
# be several times a normal poll interval so a busy worker is never flagged.
DEFAULT_HEARTBEAT_TIMEOUT_S = 120


def _project():
    from django.conf import settings  # noqa: PLC0415 - avoid requiring configured settings at import time

    return getattr(settings, "TASKDECK_PROJECT", "")


def _app_version():
    from django.conf import settings  # noqa: PLC0415 - avoid requiring configured settings at import time

    return str(getattr(settings, "VERSION", "") or "")


def baseline_p50(name, sample=20):
    """Median-ish duration of recent successful runs of this task name.

    Used so the console can say "142s, normally 38s" rather than "142s", which
    is the difference between a number and a signal. Averaged over a small
    recent sample rather than a true percentile, because computing a median in
    the database portably costs more than the precision is worth here.
    """
    recent = (
        TaskRun.objects.filter(
            name=name,
            status=TaskRunStatus.SUCCESS,
            started_at__isnull=False,
            finished_at__isnull=False,
        )
        .order_by("-finished_at")
        .values_list("started_at", "finished_at")[:sample]
    )
    durations = [(f - s).total_seconds() for s, f in recent]
    if not durations:
        return None
    durations.sort()
    return round(durations[len(durations) // 2], 2)


def serialize_run(run, entity_resolver=None):
    progress = run.resolved_progress()
    payload = {
        "id": str(run.id),
        "correlation_id": str(run.correlation_id),
        "name": run.name,
        "status": run.status,
        "started": run.started_at.isoformat() if run.started_at else None,
        "duration_s": round(run.duration_s, 2) if run.duration_s is not None else None,
        "baseline_p50_s": baseline_p50(run.name),
        "progress": ({"current": progress[0], "total": progress[1]} if progress else None),
        "message": run.message,
        "entity": None,
    }
    if run.error:
        payload["error"] = run.error[:2000]
    if entity_resolver and run.entity_type:
        try:
            payload["entity"] = entity_resolver(run.entity_type, run.entity_id)
        except Exception:  # a project's resolver touches its own
            # models and may raise anything; a broken link must degrade to no
            # link rather than take down the whole status endpoint.
            payload["entity"] = None
    return payload


def queue_snapshot():
    pending = TaskRun.objects.filter(status=TaskRunStatus.QUEUED)
    oldest = pending.order_by("enqueued_at").values_list("enqueued_at", flat=True).first()
    return {
        "pending": pending.count(),
        "oldest_pending_age_s": (
            round((timezone.now() - oldest).total_seconds(), 1) if oldest else 0
        ),
    }


def worker_snapshot(mode="sidecar", timeout_s=DEFAULT_HEARTBEAT_TIMEOUT_S):
    """Derive liveness from task heartbeats rather than from the platform.

    A process that is up but wedged must read as unhealthy, which a
    platform-level liveness probe will not catch. The cost is that a genuinely
    idle worker with no work also looks quiet — so an idle queue is reported as
    healthy, and only a *running* task that stops checking in is a fault.
    """
    now = timezone.now()
    last = (
        TaskRun.objects.filter(heartbeat_at__isnull=False)
        .order_by("-heartbeat_at")
        .values_list("heartbeat_at", flat=True)
        .first()
    )
    stuck = TaskRun.objects.filter(
        status=TaskRunStatus.RUNNING,
        heartbeat_at__lt=now - datetime.timedelta(seconds=timeout_s),
    ).count()
    return {
        "mode": mode,
        "last_seen": last.isoformat() if last else None,
        "healthy": stuck == 0,
        "stalled_runs": stuck,
    }


def collect_status(*, mode="sidecar", recent_limit=20, entity_resolver=None, schedules=None):
    """Build the full contract payload."""
    now = timezone.now()
    day_ago = now - datetime.timedelta(hours=24)

    recent = list(TaskRun.objects.select_related("parent").order_by("-enqueued_at")[:recent_limit])

    return {
        "contract_version": CONTRACT_VERSION,
        "project": _project(),
        "version": _app_version(),
        "generated_at": now.isoformat(),
        "worker": worker_snapshot(mode=mode),
        "queue": queue_snapshot(),
        "schedules": list(schedules or []),
        "recent_runs": [serialize_run(r, entity_resolver) for r in recent],
        "failures_24h": TaskRun.objects.filter(
            status__in=[TaskRunStatus.FAILED, TaskRunStatus.DEAD],
            finished_at__gte=day_ago,
        ).count(),
    }


def summary_counts(since_hours=24):
    """Per-task-name rollup, for a console overview rather than a run list."""
    since = timezone.now() - datetime.timedelta(hours=since_hours)
    return list(
        TaskRun.objects.filter(enqueued_at__gte=since)
        .values("name")
        .annotate(
            total=Count("id"),
            failed=Count("id", filter=Q(status__in=[TaskRunStatus.FAILED, TaskRunStatus.DEAD])),
            running=Count("id", filter=Q(status=TaskRunStatus.RUNNING)),
        )
        .order_by("-total")
    )
