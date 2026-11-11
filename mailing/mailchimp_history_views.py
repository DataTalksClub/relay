"""Read-only histories for exact Mailchimp source identities, including unlinked contacts."""

from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db.models import CharField, Count, F, Max, Q
from django.db.models.fields.json import KeyTextTransform, KeyTransform
from django.db.models.functions import Cast
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET

from mailing.models import EmailEvent, EmailEventType
from mailing.views import require_active_client


def source_events(client):
    events = EmailEvent.objects.filter(
        Q(metadata__erased__isnull=True) | Q(metadata__erased=False),
        Q(metadata__source_history_erased__isnull=True) | Q(metadata__source_history_erased=False),
        client=client,
        audience__organization_id=client.organization_id,
        campaign__client=client,
        campaign__audience_id=F("audience_id"),
        event_type=EmailEventType.CLICK,
        metadata__provider="mailchimp",
        metadata__historical_import=True,
    ).annotate(**identity_annotations()).order_by()
    for field, pattern in (
        ("source_datacenter", r"^[a-z]{2}[0-9]{1,30}$"),
        ("source_list_id", r"^[A-Za-z0-9_-]{1,255}$"),
        ("source_email_id", r"^[A-Za-z0-9_-]{1,255}$"),
    ):
        events = events.filter(**{field + "__regex": pattern})
    events = events.exclude(metadata__mailchimp__list_id=None).exclude(metadata__mailchimp__email_id=None)
    return events


def identity_annotations():
    return {name: Cast(KeyTextTransform(field, KeyTransform("mailchimp", "metadata")), CharField()) for name, field in (
        ("source_datacenter", "datacenter"), ("source_list_id", "list_id"), ("source_email_id", "email_id"),
    )}


def linkage_annotations():
    matched = Q(metadata__mailchimp__identity_status="matched", contact__isnull=False)
    return {
        "unlinked_clicks": Count("id", filter=Q(metadata__mailchimp__identity_status="unlinked", contact__isnull=True)),
        "matched_clicks": Count("id", filter=matched),
        "matched_contacts": Count("contact_id", filter=matched, distinct=True),
        "matched_contact_email": Max("contact__normalized_email", filter=matched),
    }


def linkage_label(row):
    if row["unlinked_clicks"] == row["click_count"]:
        return "Unlinked Mailchimp contact"
    if row["matched_clicks"] == row["click_count"] and row["matched_contacts"] == 1:
        return "Matched Relay contact"
    return "Mailchimp source identity (mixed linkage)"


def pagination_values(request, page_name):
    query = request.GET.copy()
    query.pop(page_name, None)
    return {"parameter": page_name, "querystring": query.urlencode()}


@staff_member_required
@require_GET
def mailchimp_source_contacts(request):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    linkage = "all" if request.GET.get("linkage") == "all" else "unlinked"
    events = source_events(active_client)
    if linkage == "unlinked":
        events = events.filter(contact__isnull=True, metadata__mailchimp__identity_status="unlinked")
    identities = events.values(
        "audience_id", "audience__name", "source_datacenter", "source_list_id", "source_email_id",
    ).annotate(
        click_count=Count("id"), campaign_count=Count("campaign_id", distinct=True),
        link_count=Count("url", filter=~Q(url=""), distinct=True), latest_at=Max("created_at"),
        **linkage_annotations(),
    ).order_by("-latest_at", "audience_id", "source_datacenter", "source_list_id", "source_email_id")
    page = Paginator(identities, 50).get_page(request.GET.get("page"))
    for row in page.object_list:
        row["linkage_label"] = linkage_label(row)
        row["native_contact_email"] = row["matched_contact_email"] if row["linkage_label"] == "Matched Relay contact" else None
    return render(request, "mailing/operator/mailchimp_source_contacts.html", {
        "active_client": active_client, "identities": page, "linkage": linkage,
        "pagination": pagination_values(request, "page"),
    })


@staff_member_required
@require_GET
def mailchimp_source_history(request, audience_id, datacenter, list_id, email_id):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    events = source_events(active_client).filter(
        audience_id=audience_id, metadata__mailchimp__datacenter=datacenter,
        metadata__mailchimp__list_id=list_id, metadata__mailchimp__email_id=email_id,
    )
    first = events.select_related("audience").first()
    if first is None:
        raise Http404("Mailchimp source history not found")
    summary = events.aggregate(
        click_count=Count("id"), campaign_count=Count("campaign_id", distinct=True),
        link_count=Count("url", filter=~Q(url=""), distinct=True), latest_at=Max("created_at"),
        **linkage_annotations(),
    )
    summary["linkage_label"] = linkage_label(summary)
    summary["native_contact_email"] = (
        summary["matched_contact_email"] if summary["linkage_label"] == "Matched Relay contact" else None
    )
    event_page = Paginator(events.select_related("campaign").order_by("-created_at", "-pk"), 50).get_page(
        request.GET.get("page")
    )
    campaign_page = Paginator(events.values("campaign_id", "campaign__subject").annotate(
        click_count=Count("id"), link_count=Count("url", filter=~Q(url=""), distinct=True), latest_at=Max("created_at"),
    ).order_by("-latest_at", "campaign_id"), 50).get_page(request.GET.get("campaign_page"))
    link_page = Paginator(events.exclude(url="").values("url").annotate(
        click_count=Count("id"), campaign_count=Count("campaign_id", distinct=True), latest_at=Max("created_at"),
    ).order_by("-click_count", "url"), 50).get_page(request.GET.get("link_page"))
    return render(request, "mailing/operator/mailchimp_source_history.html", {
        "active_client": active_client, "audience": first.audience,
        "source": {"datacenter": datacenter, "list_id": list_id, "email_id": email_id},
        "summary": summary, "events": event_page, "campaigns": campaign_page, "links": link_page,
        "event_pagination": pagination_values(request, "page"),
        "campaign_pagination": pagination_values(request, "campaign_page"),
        "link_pagination": pagination_values(request, "link_page"),
    })
