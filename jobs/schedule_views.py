from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from jobs.models import Schedule
from jobs.scheduling import serialize_schedule, upsert_schedule
from mailing.services.api_errors import ApiValidationError
from mailing.views import (
    authenticate_api_request,
    json_request_body,
    method_not_allowed_response,
    validation_error_response,
)


@csrf_exempt
def schedules(request):
    if request.method not in {"GET", "POST"}:
        return method_not_allowed_response(["GET", "POST"])
    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    if request.method == "GET":
        queryset = Schedule.objects.filter(client=client).order_by("name")
        return JsonResponse({"schedules": [serialize_schedule(item) for item in queryset]})

    try:
        schedule, created = upsert_schedule(json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)
    return JsonResponse(serialize_schedule(schedule), status=201 if created else 200)


@csrf_exempt
def schedule_detail(request, schedule_id):
    if request.method not in {"GET", "DELETE"}:
        return method_not_allowed_response(["GET", "DELETE"])
    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response
    schedule = Schedule.objects.filter(pk=schedule_id, client=client).first()
    if schedule is None:
        return JsonResponse({"error": {"code": "not_found"}}, status=404)
    if request.method == "DELETE":
        schedule.enabled = False
        schedule.save(update_fields=["enabled", "updated_at"])
    return JsonResponse(serialize_schedule(schedule))
