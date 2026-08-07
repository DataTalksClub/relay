from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from jobs.models import Job
from jobs.services import IdempotencyConflict, serialize_job, submit_job
from mailing.services.api_errors import ApiValidationError
from mailing.views import (
    authenticate_api_request,
    json_request_body,
    method_not_allowed_response,
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
