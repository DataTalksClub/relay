"""Read-only operator access to SES quota and usage per account+region."""

from django.http import JsonResponse

from mailing.admin_api import admin_endpoint
from mailing.services.ses_quota import ses_quota_rows


@admin_endpoint(["GET"])
def quota(request):
    return JsonResponse({"results": ses_quota_rows(refresh=request.GET.get("refresh") == "1")})
