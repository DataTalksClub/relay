"""Admin API for routing; responses and audit metadata contain no secret values."""
from django.http import JsonResponse

from mailing.admin_api import ApiError, admin_endpoint, body, get_record, record_audit
from mailing.models import InboundDelivery, InboundDomain, InboundMailbox

DOMAIN_FIELDS = ('name', 'client_id', 'from_email', 'enabled')
MAILBOX_FIELDS = ('domain_id', 'local_part', 'kind', 'enabled', 'destinations', 'sender_allowlist', 'webhook_secret_id', 'rate_per_minute')


def serialize(obj, fields):
    return {'id': obj.pk, **{field: getattr(obj, field) for field in fields}}


def write(request, model, fields, pk):
    if request.method == 'GET':
        if pk is not None:
            return JsonResponse(serialize(get_record(model, pk=pk), fields))
        return JsonResponse({'results': [serialize(row, fields) for row in model.objects.all().order_by('pk')]})
    data = body(request, fields)
    for field, value in data.items():
        expected = list if field in {'destinations', 'sender_allowlist'} else bool if field == 'enabled' else int if field in {'client_id', 'domain_id', 'rate_per_minute'} else str
        if type(value) is not expected:
            raise ApiError('validation_error', fields={field: 'invalid_type'})
    row = get_record(model, pk=pk) if pk is not None else model()
    for field, value in data.items():
        setattr(row, field, value)
    row.full_clean()
    row.save()
    record_audit(request, 'inbound_routing.update' if pk else 'inbound_routing.create', row, {'fields': sorted(data)})
    return JsonResponse(serialize(row, fields), status=200 if pk else 201)


@admin_endpoint(['GET', 'POST', 'PATCH'])
def domains(request, pk=None):
    return write(request, InboundDomain, DOMAIN_FIELDS, pk)


@admin_endpoint(['GET', 'POST', 'PATCH'])
def mailboxes(request, pk=None):
    return write(request, InboundMailbox, MAILBOX_FIELDS, pk)


@admin_endpoint(['GET'])
def deliveries(request):
    rows = InboundDelivery.objects.all().order_by('-pk')[:100]
    return JsonResponse({'results': [serialize(row, ('message_id', 'mailbox_id', 'recipient', 'destination', 'status', 'reason', 'attempt_count', 'response_status', 'outbound_message_id')) for row in rows]})
