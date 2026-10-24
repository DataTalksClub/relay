from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from mailing.admin_api import ApiError, admin_endpoint, body, get_record, record_audit
from mailing.models import Client, SendingDomain
from mailing.services.sending_domains import refresh

FIELDS = ("name", "organization_id", "role_arn", "region", "configuration_set", "enabled")


def serialize(row):
    return {
        "id": row.pk,
        **{key: getattr(row, key) for key in FIELDS},
        "client_ids": list(row.clients.values_list("pk", flat=True)),
        "verification": row.verification,
        "checked_at": row.checked_at,
    }


@admin_endpoint(["GET", "POST", "PATCH", "DELETE"])
def domains(request, pk=None):
    row = get_record(SendingDomain, pk=pk) if pk else None
    if request.method == "GET":
        return JsonResponse(serialize(row) if row else {"results": [serialize(r) for r in SendingDomain.objects.all()]})
    if request.method == "DELETE":
        if row is None:
            raise ApiError("id_required")
        # Disable instead of falling back to a different provider account.
        row.enabled = False
        row.save(update_fields=["enabled"])
        record_audit(request, "sending_domain.disable", row)
        return JsonResponse(serialize(row))
    if request.method == "PATCH" and row is None:
        raise ApiError("id_required")
    data = body(request, (*FIELDS, "client_ids"))
    row = row or SendingDomain()
    for key in FIELDS:
        if key in data:
            expected = bool if key == "enabled" else int if key == "organization_id" else str
            if type(data[key]) is not expected:
                raise ApiError("validation_error", fields={key: "invalid_type"})
            setattr(row, key, data[key])
    row.full_clean()
    client_ids = data.get("client_ids", list(row.clients.values_list("pk", flat=True)) if row.pk else [])
    if not isinstance(client_ids, list) or any(type(i) is not int for i in client_ids):
        raise ApiError("validation_error", fields={"client_ids": "must_be_integer_list"})
    clients = Client.objects.filter(pk__in=client_ids, organization_id=row.organization_id)
    if clients.count() != len(set(client_ids)):
        raise ApiError("validation_error", fields={"client_ids": "outside_organization"})
    row.save()
    row.clients.set(clients)
    refresh(row, register=True)
    record_audit(request, "sending_domain.update" if pk else "sending_domain.create", row)
    return JsonResponse(serialize(row), status=200 if pk else 201)


@admin_endpoint(["POST"])
def verify(request, pk):
    row = get_record(SendingDomain, pk=pk)
    refresh(row)
    record_audit(request, "sending_domain.verify", row)
    return JsonResponse(serialize(row))


@csrf_exempt
def client_domains(request):
    from mailing.views import (  # noqa: PLC0415 - views imports URL services
        authenticate_api_request,
        method_not_allowed_response,
    )

    client, error = authenticate_api_request(request)
    if error:
        return error
    if request.method != "GET":
        return method_not_allowed_response(["GET"])
    rows = SendingDomain.objects.filter(organization_id=client.organization_id)
    return JsonResponse(
        {
            "results": [
                serialize(row) for row in rows if not row.clients.exists() or row.clients.filter(pk=client.pk).exists()
            ]
        }
    )
