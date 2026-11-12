"""API for recipients whose send outcome is unknown (see mailing.services.send_reconcile)."""

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from mailing.models import CampaignRecipientStatus
from mailing.services.api import campaign_for_client
from mailing.services.api_errors import ApiValidationError
from mailing.services.send_reconcile import resend_unknown, unknown_recipients
from mailing.views import (
    authenticate_api_request,
    json_request_body,
    method_not_allowed_response,
    validation_error_response,
)


@csrf_exempt
def api_unknown_recipients(request, external_key):
    if request.method != "GET":
        return method_not_allowed_response(["GET"])
    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response
    try:
        campaign = campaign_for_client(external_key, request.GET, client)
    except ApiValidationError as exc:
        return validation_error_response(exc)
    return JsonResponse(unknown_recipients(campaign.pk), status=200)


@csrf_exempt
def api_unknown_resend(request, external_key):
    """POST {"recipient_ids": [...], "confirm": true}: explicit confirmation or nothing happens."""
    if request.method != "POST":
        return method_not_allowed_response(["POST"])
    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response
    try:
        data = json_request_body(request)
        campaign = campaign_for_client(external_key, data, client)
        recipient_ids = data.get("recipient_ids")
        if not isinstance(recipient_ids, list) or not all(isinstance(i, int) for i in recipient_ids):
            raise ApiValidationError({"recipient_ids": "must be a list of recipient ids"})
        resent = resend_unknown(
            campaign.pk, recipient_ids, confirm=data.get("confirm"), actor=f"api-client:{client.pk}"
        )
    except ApiValidationError as exc:
        return validation_error_response(exc)
    return JsonResponse(
        {"campaign_id": campaign.pk, "resent": resent, "status": CampaignRecipientStatus.PENDING}, status=202
    )
