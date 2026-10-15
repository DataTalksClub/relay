"""Operator-only campaign test delivery; never snapshots the live audience."""
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect
from django.tasks.exceptions import InvalidTask
from django.urls import reverse
from django.utils.html import format_html
from django.views.decorators.http import require_POST

from mailing.campaign_test_tasks import enqueue_campaign_test
from mailing.models import Campaign
from mailing.services.api import cancel_campaign_instance, validate_test_recipient_emails
from mailing.services.api_errors import ApiValidationError
from mailing.services.campaigns import campaign_send_issues


def campaign_timing_context(campaign):
    metadata = campaign.metadata if isinstance(campaign.metadata, dict) else {}
    name = metadata.get("operator_schedule_timezone", "UTC")
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        name, zone = "UTC", ZoneInfo("UTC")
    label = campaign.scheduled_at.astimezone(zone).strftime("%d %b %Y, %H:%M") + f" {name}" if campaign.scheduled_at else ""
    return {"scheduled_label": label, "scheduling_error": metadata.get("scheduling_error", "")}


@staff_member_required
@require_POST
def campaign_test_send(request, campaign_id):
    from mailing.views import require_active_client  # noqa: PLC0415 - views imports these actions through URL routing

    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    campaign = get_object_or_404(Campaign, pk=campaign_id, client=active_client)
    if request.POST.get("confirm_test") != "1":
        messages.error(request, "Confirm that these are test addresses before sending a real email.")
        return redirect("mailing:campaign_detail", campaign_id=campaign.pk)
    if campaign_send_issues(campaign):
        messages.error(request, "Complete the campaign message before sending a test.")
        return redirect("mailing:campaign_detail", campaign_id=campaign.pk)
    try:
        emails = validate_test_recipient_emails([
            value for value in re.split(r"[,;\s]+", request.POST.get("test_emails", "").strip()) if value
        ])
    except ApiValidationError:
        messages.error(request, "Enter 1–25 valid test email addresses, separated by commas or new lines.")
        return redirect("mailing:campaign_detail", campaign_id=campaign.pk)
    try:
        result = enqueue_campaign_test(campaign, emails)
    except InvalidTask:
        messages.error(request, "Tests could not be queued. Configure a deferred background worker with durable task results before sending tests.")
    else:
        text = (f"Test queued for {len(emails)} address(es). Queuing does not confirm provider acceptance. "
                f"Task {result.id}. Check worker results and the test inboxes; provider acceptance does not confirm delivery.")
        if request.user.has_perm("django_tasks_db.view_dbtaskresult"):
            result_url = reverse("admin:django_tasks_db_dbtaskresult_change", args=[result.id])
            messages.success(request, format_html('{} <a href="{}">View test task result</a>', text, result_url))
        else:
            messages.success(request, text)
    return redirect("mailing:campaign_detail", campaign_id=campaign.pk)


@staff_member_required
@require_POST
def campaign_cancel(request, campaign_id):
    from mailing.views import require_active_client  # noqa: PLC0415 - avoid routing import cycle

    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    campaign = get_object_or_404(Campaign, pk=campaign_id, client=active_client)
    if request.POST.get("confirm_cancel") != "1":
        messages.error(request, "Confirm cancellation before stopping this campaign.")
        return redirect("mailing:campaign_detail", campaign_id=campaign.pk)
    try:
        result = cancel_campaign_instance(campaign)
    except ApiValidationError:
        messages.error(request, "This campaign can no longer be cancelled. Sending has started; sent email cannot be recalled.")
    else:
        if result["cancelled"]:
            messages.success(request, "Campaign cancelled. Pending recipients will not be sent; sent email cannot be recalled.")
        else:
            messages.info(request, "This campaign was already cancelled.")
    return redirect("mailing:campaign_detail", campaign_id=campaign.pk)
