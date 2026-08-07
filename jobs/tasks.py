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
from jobs.services import TASK_TYPE_ECHO, TASK_TYPE_EMAIL_SEND, TASK_TYPE_WEBHOOK, enqueue_job
from mailing.services.transactional import TransactionalSendRejected, send_transactional_email_for_client

MAX_RESPONSE_BODY_LENGTH = 4096


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


@task()
def execute_job(job_id):
    with transaction.atomic():
        job = Job.objects.select_for_update().select_related("client", "schedule").get(pk=job_id)
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
            "X-Relay-Signature": f"sha256={signature}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=job.task["timeout_seconds"]) as response:
            response_body = response.read(MAX_RESPONSE_BODY_LENGTH + 1).decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        response_body = exc.read(MAX_RESPONSE_BODY_LENGTH + 1).decode("utf-8", "replace")
        if exc.code >= 500 or exc.code in {408, 425, 429}:
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

    if not 200 <= status < 300:
        raise RetryableJobError(
            f"webhook returned HTTP {status}",
            response_status=status,
            response_body=response_body,
        )
    return {"http_status": status}, status, response_body


def _retry_or_fail(job, exc):
    if job.attempt >= job.max_attempts:
        return _mark_failed(job, exc)

    delay = settings.RELAY_JOB_RETRY_BASE_SECONDS * (2 ** (job.attempt - 1))
    run_after = timezone.now() + timedelta(seconds=delay)
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
