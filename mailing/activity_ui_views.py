"""A common investigation entry point for transactional and campaign mail."""

from datetime import timedelta
from urllib.parse import urlencode

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import CharField, F, Q, Value
from django.db.models.functions import Coalesce, Greatest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from mailing.models import Campaign, CampaignRecipient, EmailTemplate, TransactionalMessage
from mailing.services.operator_ui import Badge, delivery_tone, humanize_metadata_value
from mailing.views import paginate, pagination_querystring, require_active_client

STATUSES = [
    ("queued", "Waiting to send"),
    ("sent", "Sent to provider"),
    ("delivered", "Delivered"),
    ("skipped", "Skipped"),
    ("failed", "Failed"),
    ("bounced", "Bounced"),
    ("complained", "Complaint"),
    ("unsubscribed", "Unsubscribed"),
]


def activity_projection(queryset, kind):
    latest = Greatest(
        F("created_at"),
        *[
            Coalesce(F(field), F("created_at"))
            for field in ("sent_at", "delivered_at", "first_opened_at", "first_clicked_at", "updated_at")
        ],
    )
    return (
        queryset.order_by()
        .annotate(
            kind=Value(kind, output_field=CharField()),
            activity_at=latest,
        )
        .values("id", "kind", "activity_at")
    )


@staff_member_required
def email_activity(request):

    client = require_active_client(request)
    if client is None:
        return redirect("mailing:dashboard")
    query = request.GET.get("q", "").strip()[:320]
    status = request.GET.get("status", "")
    status = status if status in dict(STATUSES) else ""
    period = request.GET.get("period", "")
    period = period if period in {"1", "7", "30"} else ""
    kind = request.GET.get("type", "")
    kind = kind if kind in {"campaign", "transactional"} else ""
    templates = EmailTemplate.objects.filter(client=client).order_by("name")
    campaigns = Campaign.objects.filter(client=client).order_by("-created_at")
    template = request.GET.get("template", "")
    campaign = request.GET.get("campaign", "")
    template = template if template.isdigit() and templates.filter(pk=template).exists() else ""
    campaign = campaign if campaign.isdigit() and campaigns.filter(pk=campaign).exists() else ""
    messages = TransactionalMessage.objects.filter(client=client)
    recipients = CampaignRecipient.objects.filter(campaign__client=client)
    if query:
        messages = messages.filter(
            Q(email__icontains=query) | Q(subject__icontains=query) | Q(template__name__icontains=query)
        )
        recipients = recipients.filter(Q(email__icontains=query) | Q(campaign__subject__icontains=query))
    if status == "delivered":
        messages = messages.filter(delivered_at__isnull=False)
        recipients = recipients.filter(delivered_at__isnull=False)
    elif status:
        messages = messages.filter(status=status)
        recipients = recipients.filter(status="pending" if status == "queued" else status)
    if period:
        cutoff = timezone.now() - timedelta(days=int(period))
        messages = messages.filter(created_at__gte=cutoff)
        recipients = recipients.filter(created_at__gte=cutoff)
    if template:
        messages = messages.filter(template_id=template)
        recipients = recipients.none()
    if campaign:
        recipients = recipients.filter(campaign_id=campaign)
        messages = messages.none()
    if kind == "campaign":
        messages = messages.none()
    if kind == "transactional":
        recipients = recipients.none()
    # Keep filters usable when a client has years of campaigns/templates. A
    # bookmarked selection remains available even outside the first 100 choices.
    campaigns = campaigns.filter(Q(pk__in=campaigns.values("pk")[:100]) | Q(pk=campaign or None))
    templates = templates.filter(Q(pk__in=templates.values("pk")[:100]) | Q(pk=template or None))
    combined = activity_projection(messages, "transactional").union(activity_projection(recipients, "campaign"))
    activity = paginate(request, combined.order_by("-activity_at", "-id", "kind"), per_page=25)
    projections = list(activity.object_list)
    by_message = (
        TransactionalMessage.objects.filter(pk__in=[x["id"] for x in projections if x["kind"] == "transactional"])
        .select_related("template", "contact")
        .in_bulk()
    )
    by_recipient = (
        CampaignRecipient.objects.filter(pk__in=[x["id"] for x in projections if x["kind"] == "campaign"])
        .select_related("campaign", "contact")
        .in_bulk()
    )
    rows = []
    for item in projections:
        record = by_message[item["id"]] if item["kind"] == "transactional" else by_recipient[item["id"]]
        state = "queued" if record.status == "pending" else record.status
        label = dict(STATUSES).get(state, record.get_status_display())
        if state == "sent" and record.delivered_at:
            label = "Delivered"
        if item["kind"] == "transactional":
            url = reverse("mailing:transactional_message_detail", args=[record.id])
            subject, source = record.subject, record.template.name
        else:
            url = (
                reverse("mailing:campaign_detail", args=[record.campaign_id])
                + "?"
                + urlencode({"filter": record.status, "recipient_q": record.email})
                + f"#recipient-{record.id}"
            )
            subject, source = record.campaign.subject, "Campaign"
        rows.append(
            {
                "message": record,
                "kind": item["kind"],
                "subject": subject,
                "source": source,
                "url": url,
                "badge": Badge(label, "success" if label == "Delivered" else delivery_tone(state)),
                "activity_at": item["activity_at"],
                "issue": humanize_metadata_value("reason", record.last_error) if record.last_error else "",
            }
        )
    return render(
        request,
        "mailing/operator/email_activity.html",
        {
            "active_client": client,
            "activity": activity,
            "message_rows": rows,
            "query": query,
            "status": status,
            "period": period,
            "kind": kind,
            "template_filter": template,
            "campaign_filter": campaign,
            "templates": templates,
            "campaigns": campaigns,
            "status_options": STATUSES,
            "pagination_querystring": pagination_querystring(request),
        },
    )
