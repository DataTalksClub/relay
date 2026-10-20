"""Read-only delivery counts for recipient-list transactional campaigns."""
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_safe

from mailing.models import TransactionalCampaign, TransactionalMessageStatus


@staff_member_required
@require_safe
def detail(request, campaign_id):
    # Staff operators can follow an exact external link without first selecting
    # a workspace. This page is read-only and never queues mail.
    campaign = get_object_or_404(TransactionalCampaign.objects.select_related("client", "recipient_list"), pk=campaign_id)
    counts = campaign.messages.aggregate(
        total=Count("id"),
        delivered=Count("id", filter=Q(delivered_at__isnull=False)),
        **{status: Count("id", filter=Q(status=status)) for status in TransactionalMessageStatus.values},
    )
    return render(request, "mailing/operator/transactional_campaign_detail.html", {
        "campaign": campaign,
        "counts": counts,
        "status_counts": [{"label": label, "count": counts[status]} for status, label in TransactionalMessageStatus.choices],
    })


@staff_member_required
@require_safe
def lookup(request):
    campaign = TransactionalCampaign.objects.filter(
        client__slug=request.GET.get("client", ""),
        idempotency_key=request.GET.get("idempotency_key", ""),
    ).first()
    if campaign is not None:
        return redirect("mailing:transactional_campaign_detail", campaign_id=campaign.pk)
    return render(request, "mailing/operator/transactional_campaign_waiting.html")
