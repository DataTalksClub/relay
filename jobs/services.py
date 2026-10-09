import datetime
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

import taskdeck
from jobs.models import SCHEDULER_HEARTBEAT_ID, Job, JobStatus, SchedulerHeartbeat
from mailing.services.api_errors import ApiValidationError
from taskdeck.collector import DEFAULT_HEARTBEAT_TIMEOUT_S

TASK_TYPE_ECHO = "system.echo"
TASK_TYPE_EMAIL_SEND = "email.send"
TASK_TYPE_WEBHOOK = "webhook"
TASK_TYPES = frozenset({TASK_TYPE_ECHO, TASK_TYPE_EMAIL_SEND, TASK_TYPE_WEBHOOK})


class IdempotencyConflict(Exception):
    pass


@dataclass(frozen=True)
class Submission:
    task_type: str
    idempotency_key: str
    correlation_id: uuid.UUID
    task: dict
    max_attempts: int
    request_hash: str


def normalize_submission(data, client) -> Submission:
    errors = {}
    task_type = data.get("type")
    if task_type not in TASK_TYPES:
        errors["type"] = "unsupported"

    idempotency_key = data.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        errors["idempotency_key"] = "required"
        idempotency_key = ""
    elif len(idempotency_key.strip()) > 255:
        errors["idempotency_key"] = "too_long"
    else:
        idempotency_key = idempotency_key.strip()

    correlation_id = data.get("correlation_id") or uuid.uuid4()
    try:
        correlation_id = uuid.UUID(str(correlation_id))
    except (TypeError, ValueError, AttributeError):
        errors["correlation_id"] = "invalid_uuid"
        correlation_id = uuid.uuid4()

    max_attempts = data.get("max_attempts", settings.RELAY_JOB_MAX_ATTEMPTS)
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 10:
        errors["max_attempts"] = "must_be_integer_between_1_and_10"
        max_attempts = settings.RELAY_JOB_MAX_ATTEMPTS

    task_payload = _normalize_task_payload(task_type, data, client, errors)
    if errors:
        raise ApiValidationError(errors)

    canonical = {
        "type": task_type,
        "task": task_payload,
        "max_attempts": max_attempts,
    }
    request_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return Submission(
        task_type=task_type,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        task=task_payload,
        max_attempts=max_attempts,
        request_hash=request_hash,
    )


def _normalize_task_payload(task_type, data, client, errors):
    params = data.get("params", {})
    if not isinstance(params, dict):
        errors["params"] = "must_be_object"
        params = {}

    if task_type == TASK_TYPE_ECHO:
        return {"params": params}

    if task_type == TASK_TYPE_EMAIL_SEND:
        if not params:
            errors["params"] = "required"
        return {"params": params}

    if task_type != TASK_TYPE_WEBHOOK:
        return {}

    url = data.get("url")
    if not isinstance(url, str) or not url.strip():
        errors["url"] = "required"
        url = ""
    else:
        url = url.strip()
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        allowed_origins = {
            str(value).rstrip("/").lower()
            for value in (client.relay_webhook_allowed_origins or [])
        }
        if parsed.scheme != "https" or not parsed.netloc or origin not in allowed_origins:
            errors["url"] = "origin_not_allowed"

    if not client.relay_webhook_signing_secret:
        errors["type"] = "webhook_signing_secret_not_configured"

    timeout = data.get("timeout_seconds", settings.RELAY_WEBHOOK_TIMEOUT_SECONDS)
    maximum = settings.RELAY_WEBHOOK_MAX_TIMEOUT_SECONDS
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or timeout <= 0
        or timeout > maximum
    ):
        errors["timeout_seconds"] = f"must_be_between_0_and_{maximum:g}"
        timeout = settings.RELAY_WEBHOOK_TIMEOUT_SECONDS

    return {"url": url, "payload": params, "timeout_seconds": float(timeout)}


def submit_job(data, client, *, schedule=None):
    submission = normalize_submission(data, client)
    with transaction.atomic():
        # A replay of an existing idempotency key returns the original task
        # and never counts against the per-client webhook limits.
        replayed = Job.objects.filter(
            client=client, idempotency_key=submission.idempotency_key
        ).exists()
        if not replayed and submission.task_type == TASK_TYPE_WEBHOOK and schedule is None:
            _enforce_webhook_client_limits(client)
        job, created = Job.objects.get_or_create(
            client=client,
            idempotency_key=submission.idempotency_key,
            defaults={
                "schedule": schedule,
                "task_type": submission.task_type,
                "request_hash": submission.request_hash,
                "correlation_id": submission.correlation_id,
                "task": submission.task,
                "max_attempts": submission.max_attempts,
            },
        )
        if not created and job.request_hash != submission.request_hash:
            raise IdempotencyConflict
        if created:
            transaction.on_commit(lambda: enqueue_job(job.pk))
    return job, created


def _enforce_webhook_client_limits(client):
    """Reject webhook submissions when the client is over its limits.

    Schedule-created jobs skip these checks: they are operator-configured,
    bounded by cron, and a dropped tick is recorded as missed rather than
    erroring the scheduler loop.
    """
    inflight_limit = settings.RELAY_WEBHOOK_MAX_INFLIGHT_PER_CLIENT
    inflight = Job.objects.filter(
        client=client,
        task_type=TASK_TYPE_WEBHOOK,
        status__in=[JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.RETRYING],
    ).count()
    if inflight >= inflight_limit:
        raise ApiValidationError(
            {"type": "concurrency_limit_exceeded"},
            status_code=429,
        )

    rate_limit = settings.RELAY_WEBHOOK_RATE_LIMIT_PER_MINUTE
    window_start = timezone.now() - timedelta(seconds=60)
    recent = Job.objects.filter(
        client=client,
        task_type=TASK_TYPE_WEBHOOK,
        created_at__gte=window_start,
    ).count()
    if recent >= rate_limit:
        raise ApiValidationError(
            {"type": "rate_limit_exceeded"},
            status_code=429,
        )


def enqueue_job(job_id):
    from jobs.tasks import execute_job  # noqa: PLC0415 - avoids service/task import cycle

    job = Job.objects.get(pk=job_id)
    if job.is_terminal:
        return None
    result = execute_job.using(run_after=job.run_after).enqueue(str(job.pk))
    Job.objects.filter(pk=job.pk).update(task_result_id=str(result.id))
    taskdeck.stamp(
        result,
        entity=("relay_job", str(job.pk)),
        owner_id=str(job.client_id),
        message=job.task_type,
    )
    return result


def serialize_job(job):
    return {
        "id": str(job.pk),
        "type": job.task_type,
        "idempotency_key": job.idempotency_key,
        "correlation_id": str(job.correlation_id),
        "status": job.status,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "run_after": job.run_after.isoformat(),
        "lease_expires_at": job.lease_expires_at.isoformat() if job.lease_expires_at else None,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "response_status": job.response_status,
        "response_body": job.response_body,
        "result": job.result,
        "error": job.error,
        "schedule_id": str(job.schedule_id) if job.schedule_id else None,
    }


TASK_PAGE_SIZE = 100


def validate_task_limit(value):
    """Same 1-100 range as the other client list endpoints.

    The default is the cap the task list has always had, so a caller that sends
    no query at all gets exactly what it got before.
    """
    if value in (None, ""):
        return TASK_PAGE_SIZE
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiValidationError({"limit": "must_be_integer"}) from exc
    if limit < 1 or limit > TASK_PAGE_SIZE:
        raise ApiValidationError({"limit": "must_be_between_1_and_100"})
    return limit


def validate_task_status(value):
    if value in (None, ""):
        return None
    if value not in JobStatus.values:
        raise ApiValidationError({"status": "unknown"})
    return value


def validate_task_type(value):
    if value in (None, ""):
        return None
    if value not in TASK_TYPES:
        raise ApiValidationError({"task_type": "unknown"})
    return value


def encode_task_cursor(job):
    """Position an opaque cursor at one job.

    Jobs are keyed by UUID, so there is no ordered key to page on; the pair
    (created_at, id) is what makes the order total and the page boundary stable
    under a concurrent submission.
    """
    return f"{job.created_at.isoformat()}|{job.pk}"


def decode_task_cursor(value):
    if value in (None, ""):
        return None
    try:
        raw, _, job_id = value.rpartition("|")
        return datetime.datetime.fromisoformat(raw), uuid.UUID(job_id)
    except (TypeError, ValueError) as exc:
        raise ApiValidationError({"cursor": "invalid"}) from exc


def task_page(data, client):
    """One page of a client's tasks, newest first.

    Cursor pagination rather than an offset: the list is ordered by creation
    time, a busy client submits while a consumer pages, and an offset silently
    skips or repeats a row when it does.
    """
    limit = validate_task_limit(data.get("limit"))
    status = validate_task_status(data.get("status"))
    task_type = validate_task_type(data.get("task_type"))
    cursor = decode_task_cursor(data.get("cursor"))

    queryset = Job.objects.filter(client=client)
    if status:
        queryset = queryset.filter(status=status)
    if task_type:
        queryset = queryset.filter(task_type=task_type)
    if cursor is not None:
        created_at, job_id = cursor
        queryset = queryset.filter(
            Q(created_at__lt=created_at) | (Q(created_at=created_at) & Q(id__lt=job_id))
        )
    items = list(queryset.order_by("-created_at", "-id")[: limit + 1])
    next_cursor = encode_task_cursor(items[limit - 1]) if len(items) > limit else None
    return items[:limit], next_cursor


def retry_delay(attempt):
    """Backoff before the redelivery that follows ``attempt``."""
    return settings.RELAY_JOB_RETRY_BASE_SECONDS * (2 ** (attempt - 1))


def recover_unenqueued_jobs(*, limit=100):
    recovered = 0
    due = Job.objects.filter(
        status__in=[JobStatus.QUEUED, JobStatus.RETRYING],
        run_after__lte=timezone.now(),
        task_result_id="",
    ).order_by("run_after")[:limit]
    for job in due:
        if enqueue_job(job.pk) is not None:
            recovered += 1
    return recovered


def fail_expired_leases(*, limit=100):
    """Fail running lease jobs whose completion callback never arrived.

    The receiver took the work with a 202 ack; re-executing it could repeat a
    side effect the client already performed, so an expired lease is terminal,
    not a retry.
    """
    now = timezone.now()
    expired = Job.objects.filter(
        status=JobStatus.RUNNING,
        lease_expires_at__lt=now,
    ).values_list("pk", flat=True)[:limit]
    failed = 0
    for job_id in expired:
        failed += Job.objects.filter(
            pk=job_id,
            status=JobStatus.RUNNING,
            lease_expires_at__lt=now,
        ).update(
            status=JobStatus.FAILED,
            error="lease expired without a completion callback",
            finished_at=now,
            run_after=now,
        )
    return failed


def latest_heartbeat():
    """The scheduler's most recent tick, or None when it has never ticked."""
    return SchedulerHeartbeat.objects.order_by("-ticked_at").first()


def heartbeat_state():
    """The latest heartbeat plus whether it has gone stale.

    Staleness is judged against the timeout the status contract already uses,
    so a page that reads healthy and a console that reads unhealthy cannot both
    be right about the same loop.
    """
    row = latest_heartbeat()
    if row is None:
        return None, None
    stale = row.ticked_at < timezone.now() - timedelta(seconds=DEFAULT_HEARTBEAT_TIMEOUT_S)
    return row, stale


def record_scheduler_heartbeat(
    *, schedules_fired=0, expired_leases=0, recovered_jobs=0, campaigns_dispatched=0
):
    """Stamp the heartbeat and add this pass's counters to the running totals.

    Written in its own short transaction, deliberately outside the per-schedule
    atomic block in ``run_due_schedules``: the tick has to survive a schedule
    whose own transaction rolled back, and it must not hold a lock while the
    next pass takes its ``select_for_update``. A single UPDATE is the whole cost,
    once per pass, and it commits whether or not anything fired.
    """
    pid = os.getpid()
    fresh = {
        "pid": pid,
        "ticked_at": timezone.now(),
        "schedules_fired": schedules_fired,
        "expired_leases": expired_leases,
        "recovered_jobs": recovered_jobs,
        "campaigns_dispatched": campaigns_dispatched,
    }
    if SchedulerHeartbeat.objects.filter(pk=SCHEDULER_HEARTBEAT_ID).first() is None:
        # get_or_create rather than check-then-create: two schedulers starting
        # at once (a rolling deploy overlaps them) must not race into a
        # duplicate-key IntegrityError that takes the loop down.
        SchedulerHeartbeat.objects.get_or_create(pk=SCHEDULER_HEARTBEAT_ID, defaults=fresh)
        return
    # A different pid means a new container: its totals start from this pass
    # rather than inheriting numbers from a process that no longer exists.
    if latest_heartbeat().pid != pid:
        SchedulerHeartbeat.objects.update_or_create(
            pk=SCHEDULER_HEARTBEAT_ID, defaults=fresh
        )
        return
    SchedulerHeartbeat.objects.filter(pk=SCHEDULER_HEARTBEAT_ID).update(
        ticked_at=timezone.now(),
        schedules_fired=F("schedules_fired") + schedules_fired,
        expired_leases=F("expired_leases") + expired_leases,
        recovered_jobs=F("recovered_jobs") + recovered_jobs,
        campaigns_dispatched=F("campaigns_dispatched") + campaigns_dispatched,
    )
