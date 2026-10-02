"""Token-gated copy of mailing data from one Relay host to another.

Unset RELAY_TRANSFER_TOKEN and both routes 404. The document includes API key
hashes and client webhook secrets, so the token stays off except while a
migration is running.
"""

import hmac
import json

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from mailing.services.transfer import MAX_PAGE, TransferError, export_page, load_page, manifest


def _authorised(request):
    expected = settings.RELAY_TRANSFER_TOKEN
    if not expected:
        return None
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if header.startswith(prefix) and hmac.compare_digest(header[len(prefix) :].strip(), expected):
        return True
    return False


def _denied(request):
    if not settings.RELAY_TRANSFER_TOKEN:
        return JsonResponse({"error": {"code": "not_found"}}, status=404)
    return JsonResponse({"error": {"code": "unauthorized"}}, status=401)


@csrf_exempt
def transfer_export(request):
    allowed = _authorised(request)
    if allowed is not True:
        return _denied(request)
    if request.method != "GET":
        return JsonResponse({"error": {"code": "method_not_allowed"}}, status=405)
    resource = request.GET.get("resource", "").strip()
    if not resource:
        return JsonResponse(manifest())
    try:
        offset = int(request.GET.get("offset", "0"))
        limit = int(request.GET.get("limit", "200"))
    except ValueError:
        return JsonResponse({"error": {"code": "validation_error", "message": "invalid page"}}, status=400)
    if offset < 0 or limit < 1:
        return JsonResponse({"error": {"code": "validation_error", "message": "invalid page"}}, status=400)
    try:
        payload = export_page(resource, offset, min(limit, MAX_PAGE))
    except TransferError as exc:
        return JsonResponse({"error": {"code": "validation_error", "message": exc.message}}, status=400)
    return JsonResponse(payload)


@csrf_exempt
def transfer_load(request):
    allowed = _authorised(request)
    if allowed is not True:
        return _denied(request)
    if request.method != "POST":
        return JsonResponse({"error": {"code": "method_not_allowed"}}, status=405)
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": {"code": "validation_error", "message": "invalid json"}}, status=400)
    try:
        result = load_page(payload)
    except TransferError as exc:
        return JsonResponse({"error": {"code": "validation_error", "message": exc.message}}, status=400)
    return JsonResponse(result)
