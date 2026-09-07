from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from jobs.models import Job, JobStatus
from jobs.services import (
    IdempotencyConflict,
    enqueue_job,
    retry_delay,
    serialize_job,
    submit_job,
)
from mailing.services.api_errors import ApiValidationError
from mailing.views import (
    authenticate_api_request,
    json_request_body,
    method_not_allowed_response,
    paginate,
    pagination_querystring,
    validation_error_response,
)


@csrf_exempt
def tasks(request):
    if request.method not in {"GET", "POST"}:
        return method_not_allowed_response(["GET", "POST"])
    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    if request.method == "GET":
        jobs = Job.objects.filter(client=client).order_by("-created_at")[:100]
        return JsonResponse({"tasks": [serialize_job(job) for job in jobs]})

    try:
        job, created = submit_job(json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)
    except IdempotencyConflict:
        return JsonResponse(
            {
                "error": {
                    "code": "idempotency_conflict",
                    "message": "The idempotency key was already used for a different task.",
                }
            },
            status=409,
        )
    job.refresh_from_db()
    payload = serialize_job(job) | {"idempotent_replay": not created}
    return JsonResponse(payload, status=202 if created else 200)


def task_detail(request, task_id):
    if request.method != "GET":
        return method_not_allowed_response(["GET"])
    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response
    job = Job.objects.filter(pk=task_id, client=client).first()
    if job is None:
        return JsonResponse({"error": {"code": "not_found"}}, status=404)
    return JsonResponse(serialize_job(job))


@csrf_exempt
@require_POST
def task_complete(request, task_id):
    return _resolve_lease(request, task_id, resolution=JobStatus.SUCCEEDED)


@csrf_exempt
@require_POST
def task_fail(request, task_id):
    return _resolve_lease(request, task_id, resolution=JobStatus.FAILED)


def _resolve_lease(request, task_id, *, resolution):
    """Apply a client's ack-then-callback completion for a leased task."""
    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        body = json_request_body(request)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    retryable = False
    if resolution == JobStatus.FAILED:
        error = body.get("error", "failed by client callback")
        if not isinstance(error, str):
            return JsonResponse(
                {"error": {"code": "validation_error", "fields": {"error": "must_be_string"}}},
                status=400,
            )
        retryable = body.get("retryable", False)
        if not isinstance(retryable, bool):
            return JsonResponse(
                {"error": {"code": "validation_error", "fields": {"retryable": "must_be_boolean"}}},
                status=400,
            )
        result = None
    else:
        error = ""
        result = body.get("result")

    with transaction.atomic():
        job = Job.objects.select_for_update().filter(pk=task_id, client=client).first()
        if job is None:
            return JsonResponse({"error": {"code": "not_found"}}, status=404)

        conflict = _lease_conflict(job, resolution)
        if conflict:
            return conflict

        if resolution == JobStatus.FAILED and retryable and job.attempt < job.max_attempts:
            # A transient failure the receiver asked to have redelivered:
            # re-enter the normal backoff schedule while attempts remain.
            Job.objects.filter(pk=job.pk, status=JobStatus.RUNNING).update(
                status=JobStatus.RETRYING,
                run_after=timezone.now() + timedelta(seconds=retry_delay(job.attempt)),
                task_result_id="",
                error=error[:4000],
                lease_expires_at=None,
            )
            job.refresh_from_db()
            transaction.on_commit(lambda: enqueue_job(job.pk))
            return JsonResponse(serialize_job(job))

        now = timezone.now()
        job.status = resolution
        job.finished_at = now
        job.run_after = now
        job.error = error[:4000]
        if result is not None:
            job.result = result
        job.lease_expires_at = None
        job.save(
            update_fields=[
                "status",
                "finished_at",
                "run_after",
                "error",
                "result",
                "lease_expires_at",
                "updated_at",
            ]
        )
    return JsonResponse(serialize_job(job))


def _lease_conflict(job, resolution):
    if job.status == resolution:
        # Repeating the same callback is harmless; say so instead of 409.
        response = JsonResponse(serialize_job(job))
        response["X-Idempotent-Replay"] = "true"
        return response
    if job.is_terminal:
        return JsonResponse(
            {
                "error": {
                    "code": "task_already_finished",
                    "message": f"The task already finished as {job.status}.",
                }
            },
            status=409,
        )
    if job.status != JobStatus.RUNNING or job.lease_expires_at is None:
        return JsonResponse(
            {
                "error": {
                    "code": "no_lease_in_progress",
                    "message": "The task is not waiting for a completion callback.",
                }
            },
            status=409,
        )
    if job.lease_expires_at < timezone.now():
        # Fail it here rather than 409: the lease is over either way, and the
        # sweep's conditional update stays a no-op.
        Job.objects.filter(pk=job.pk, status=JobStatus.RUNNING).update(
            status=JobStatus.FAILED,
            error="lease expired without a completion callback",
            finished_at=timezone.now(),
        )
        job.refresh_from_db()
        return JsonResponse(
            {
                "error": {
                    "code": "lease_expired",
                    "message": "The lease expired before the callback arrived.",
                }
            },
            status=409,
        )
    return None


DEAD_LETTER_PAGE_SIZE = 25


@staff_member_required
def dead_letter_list(request):
    failed = (
        Job.objects.filter(status=JobStatus.FAILED)
        .select_related("client", "schedule")
        .order_by("-finished_at", "-updated_at")
    )
    return render(
        request,
        "jobs/dead_letters.html",
        {
            "page": paginate(request, failed, per_page=DEAD_LETTER_PAGE_SIZE),
            "pagination_querystring": pagination_querystring(request),
        },
    )


@staff_member_required
@require_POST
def dead_letter_retry(request, task_id):
    job = get_object_or_404(Job, pk=task_id, status=JobStatus.FAILED)
    with transaction.atomic():
        Job.objects.filter(pk=job.pk, status=JobStatus.FAILED).update(
            status=JobStatus.QUEUED,
            attempt=0,
            run_after=timezone.now(),
            task_result_id="",
            error="",
            response_status=None,
            response_body="",
            lease_expires_at=None,
        )
        transaction.on_commit(lambda: enqueue_job(job.pk))
    return redirect("jobs:dead_letter_list")
