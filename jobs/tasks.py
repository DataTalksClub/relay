import hashlib
import hmac
import json
import socket
import urllib.error
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.tasks import task
from django.utils import timezone

import taskdeck
from jobs.models import Job, JobStatus
from jobs.services import TASK_TYPE_ECHO, TASK_TYPE_EMAIL_SEND, TASK_TYPE_WEBHOOK, enqueue_job, retry_delay
from mailing.services.transactional import TransactionalSendRejected, send_transactional_email_for_client

MAX_RESPONSE_BODY_LENGTH = 2048
HTTP_LEASE_ACK_STATUS = 202


class RetryableJobError(Exception):
    def __init__(self, message, *, response_status=None, response_body=""):
        self.response_status = response_status
        self.response_body = response_body
        super().__init__(message)


class PermanentJobError(Exception):
    def __init__(self, message, *, response_status=None, response_body=""):
        self.response_status = response_status
        self.response_body = response_body
        super().__init__(message)


class WebhookAcknowledged(Exception):
    """The receiver accepted the work with a 202 lease instead of finishing it.

    Carries the lease the receiver asked for; `execute_job` turns it into a
    running lease that the complete/fail callbacks resolve.
    """

    def __init__(self, lease_seconds, response_body=""):
        self.lease_seconds = lease_seconds
        self.response_body = response_body
        super().__init__(f"webhook acknowledged with lease of {lease_seconds} seconds")


@task()
def execute_job(job_id):
    with transaction.atomic():
        job = Job.objects.select_for_update().select_related("client").get(pk=job_id)
        if job.is_terminal:
            return {"job_id": str(job.pk), "status": job.status}
        job.attempt += 1
        job.status = JobStatus.RUNNING
        job.started_at = job.started_at or timezone.now()
        job.error = ""
        job.save(update_fields=["attempt", "status", "started_at", "error", "updated_at"])

    taskdeck.set_owner(str(job.client_id))
    taskdeck.set_entity("relay_job", str(job.pk))

    try:
        result, response_status, response_body = _dispatch(job)
    except WebhookAcknowledged as ack:
        return _start_lease(job, ack)
    except RetryableJobError as exc:
        return _retry_or_fail(job, exc)
    except PermanentJobError as exc:
        return _mark_failed(job, exc)
    except Exception as exc:
        return _retry_or_fail(job, RetryableJobError(f"{type(exc).__name__}: {exc}"))

    finished_at = timezone.now()
    Job.objects.filter(pk=job.pk).update(
        status=JobStatus.SUCCEEDED,
        result=result,
        response_status=response_status,
        response_body=response_body[:MAX_RESPONSE_BODY_LENGTH],
        finished_at=finished_at,
        run_after=finished_at,
        lease_expires_at=None,
        error="",
    )
    if job.schedule_id:
        job.schedule.__class__.objects.filter(pk=job.schedule_id).update(
            last_success_at=finished_at
        )
    return {"job_id": str(job.pk), "status": JobStatus.SUCCEEDED, "result": result}


def _dispatch(job):
    if job.task_type == TASK_TYPE_ECHO:
        return job.task["params"], None, ""
    if job.task_type == TASK_TYPE_EMAIL_SEND:
        try:
            payload = send_transactional_email_for_client(job.task["params"], job.client)
        except TransactionalSendRejected as exc:
            raise PermanentJobError(json.dumps(exc.payload, sort_keys=True)) from exc
        return payload, None, ""
    if job.task_type == TASK_TYPE_WEBHOOK:
        return _call_webhook(job)
    raise PermanentJobError(f"unsupported task type: {job.task_type}")


def _call_webhook(job):
    body = json.dumps(job.task["payload"], sort_keys=True, separators=(",", ":")).encode()
    timestamp = str(int(timezone.now().timestamp()))
    signature = hmac.new(
        job.client.relay_webhook_signing_secret.encode(),
        timestamp.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    request = urllib.request.Request(
        job.task["url"],
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "relay/1",
            "X-Relay-Timestamp": timestamp,
            "X-Relay-Task-Id": str(job.pk),
            "X-Relay-Correlation-Id": str(job.correlation_id),
            "X-Relay-Attempt": str(job.attempt),
            "X-Relay-Signature": f"sha256={signature}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=job.task["timeout_seconds"]) as response:
            response_body = response.read(MAX_RESPONSE_BODY_LENGTH + 1).decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        response_body = exc.read(MAX_RESPONSE_BODY_LENGTH + 1).decode("utf-8", "replace")
        if exc.code >= 500 or exc.code == 429:
            raise RetryableJobError(
                f"webhook returned HTTP {exc.code}",
                response_status=exc.code,
                response_body=response_body,
            ) from exc
        raise PermanentJobError(
            f"webhook returned HTTP {exc.code}",
            response_status=exc.code,
            response_body=response_body,
        ) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise RetryableJobError(f"webhook request failed: {exc}") from exc

    if status == HTTP_LEASE_ACK_STATUS:
        lease_seconds = _lease_seconds_from(response_body)
        raise WebhookAcknowledged(lease_seconds, response_body=response_body)

    if not 200 <= status < 300:
        raise RetryableJobError(
            f"webhook returned HTTP {status}",
            response_status=status,
            response_body=response_body,
        )
    return {"http_status": status}, status, response_body


def _lease_seconds_from(response_body):
    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise PermanentJobError("webhook returned 202 without a JSON lease body") from exc
    lease_seconds = parsed.get("lease_seconds") if isinstance(parsed, dict) else None
    maximum = settings.RELAY_WEBHOOK_MAX_LEASE_SECONDS
    if (
        not isinstance(lease_seconds, (int, float))
        or isinstance(lease_seconds, bool)
        or lease_seconds <= 0
        or lease_seconds > maximum
    ):
        raise PermanentJobError(
            f"webhook returned 202 with lease_seconds outside 0 < N <= {maximum:g}"
        )
    return float(lease_seconds)


def _start_lease(job, ack):
    lease_expires_at = timezone.now() + timedelta(seconds=ack.lease_seconds)
    Job.objects.filter(pk=job.pk).update(
        status=JobStatus.RUNNING,
        lease_expires_at=lease_expires_at,
        response_status=HTTP_LEASE_ACK_STATUS,
        response_body=ack.response_body[:MAX_RESPONSE_BODY_LENGTH],
        error="",
    )
    return {
        "job_id": str(job.pk),
        "status": JobStatus.RUNNING,
        "lease_expires_at": lease_expires_at.isoformat(),
        "lease_seconds": ack.lease_seconds,
    }


def _retry_or_fail(job, exc):
    if job.attempt >= job.max_attempts:
        return _mark_failed(job, exc)

    run_after = timezone.now() + timedelta(seconds=retry_delay(job.attempt))
    Job.objects.filter(pk=job.pk).update(
        status=JobStatus.RETRYING,
        run_after=run_after,
        task_result_id="",
        response_status=exc.response_status,
        response_body=exc.response_body[:MAX_RESPONSE_BODY_LENGTH],
        error=str(exc)[:4000],
    )
    transaction.on_commit(lambda: enqueue_job(job.pk))
    return {
        "job_id": str(job.pk),
        "status": JobStatus.RETRYING,
        "run_after": run_after.isoformat(),
    }


def _mark_failed(job, exc):
    finished_at = timezone.now()
    Job.objects.filter(pk=job.pk).update(
        status=JobStatus.FAILED,
        finished_at=finished_at,
        run_after=finished_at,
        response_status=exc.response_status,
        response_body=exc.response_body[:MAX_RESPONSE_BODY_LENGTH],
        error=str(exc)[:4000],
    )
    return {"job_id": str(job.pk), "status": JobStatus.FAILED, "error": str(exc)}
