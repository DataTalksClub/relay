"""Audience task screens with scope-preserving links and policy-based summaries."""

from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Q
from django.urls import reverse

from mailing.filter_navigation import removable_contact_filters
from mailing.models import (
    CampaignRecipient,
    CampaignRecipientSkipReason,
    CampaignRecipientStatus,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailValidationStatus,
    Subscription,
    Tag,
)
from mailing.services.contacts import NON_DELIVERABLE_EMAIL_VALIDATION_STATUSES
from mailing.services.operator_management import latest_audits_for
from mailing.services.operator_ui import (
    Stat,
    audience_campaign_history,
    audience_recent_events,
    choices_from_text_choices,
    contact_explorer_options,
    contact_explorer_queryset,
    contact_result_rows,
    count_by_field,
    email_validation_label,
    event_context,
    metadata_summary,
    parse_contact_explorer_filters,
)

SECTIONS = [
    ("members", "Members"),
    ("segments", "Segments"),
    ("campaigns", "Campaigns"),
    ("activity", "Activity"),
    ("health", "Health"),
]
FILTER_KEYS = {
    "Email": "q",
    "Subscription": "subscription_status",
    "Verification": "verified",
    "Validation": "email_validation_status",
    "Suppression": "suppression",
    "Campaign": "campaign_status",
    "Skip reason": "skip_reason",
    "Engagement": "engagement",
    "Includes tag": "include_tags",
    "Excludes tag": "exclude_tags",
}


def section_links(request, base_url, selected):
    links = []
    for key, label in SECTIONS:
        params = request.GET.copy()
        params.pop("page", None)
        params["section"] = key
        links.append(
            {"key": key, "label": label, "url": base_url + "?" + params.urlencode(), "current": key == selected}
        )
    return links


def removable_filters(request, filters, base_url):
    chips = [
        {"label": row["label"], "value": row["value"], "url": base_url + row["remove_url"]}
        for row in removable_contact_filters(request, filters)
        if row["label"] not in {"Audience", "Client"} and row["remove_url"]
    ]
    if request.GET.get("eligibility") in {"eligible", "excluded"}:
        params = request.GET.copy()
        params.pop("eligibility", None)
        params.pop("page", None)
        params["section"] = "members"
        chips.append(
            {
                "label": "Eligibility",
                "value": request.GET["eligibility"].capitalize(),
                "url": base_url + "?" + params.urlencode(),
            }
        )
    return chips


def eligibility_summary(audience, client):
    """Same consent, suppression, verification, and validation rules as member rows.

    This is an unsegmented member count. Campaign category preferences and tag
    filters apply later; it is deliberately not presented as a campaign estimate.
    """
    subscriptions = Subscription.objects.filter(contact_id=OuterRef("pk"), audience=audience)
    client_subscriptions = subscriptions.filter(client=client)
    audience_subscriptions = subscriptions.filter(client__isnull=True)
    contacts = Contact.objects.filter(Exists(client_subscriptions))
    eligible = (
        contacts.filter(Exists(client_subscriptions.filter(status="subscribed")))
        .filter(
            Q(verified_at__isnull=False)
            | Q(Exists(client_subscriptions.filter(verified_at__isnull=False)))
            | Q(Exists(audience_subscriptions.filter(verified_at__isnull=False)))
        )
        .filter(global_unsubscribed_at__isnull=True, hard_bounced_at__isnull=True, complained_at__isnull=True)
        .exclude(email_validation_status__in=NON_DELIVERABLE_EMAIL_VALIDATION_STATUSES)
        .exclude(Exists(audience_subscriptions.filter(status="unsubscribed")))
    )
    total = contacts.count()
    eligible_count = eligible.count()
    excluded = contacts.exclude(pk__in=eligible.values("pk"))
    return (
        {"total": total, "eligible": eligible_count, "excluded": total - eligible_count},
        eligible.values("pk"),
        excluded.values("pk"),
    )


def scoped_health_summary(audience, client):
    subscriptions = Subscription.objects.filter(contact_id=OuterRef("pk"), audience=audience, client=client)
    contacts = Contact.objects.filter(Exists(subscriptions))
    recipients = CampaignRecipient.objects.filter(
        contact_id=OuterRef("pk"), campaign__audience=audience, campaign__client=client
    )
    events = EmailEvent.objects.filter(contact_id=OuterRef("pk"), audience=audience, client=client)
    sent = recipients.filter(Q(sent_at__isnull=False) | Q(status="sent"))
    opened = Q(Exists(recipients.filter(first_opened_at__isnull=False))) | Q(
        Exists(events.filter(event_type=EmailEventType.OPEN))
    )
    clicked = Q(Exists(recipients.filter(first_clicked_at__isnull=False))) | Q(
        Exists(events.filter(event_type=EmailEventType.CLICK))
    )
    definitions = [
        ("members", "Members", Q(), ""),
        (
            "subscribed",
            "Subscribed",
            Q(Exists(subscriptions.filter(status="subscribed"))),
            "subscription_status=subscribed",
        ),
        ("pending", "Pending", Q(Exists(subscriptions.filter(status="pending"))), "subscription_status=pending"),
        (
            "unsubscribed",
            "Unsubscribed",
            Q(Exists(subscriptions.filter(status="unsubscribed"))),
            "subscription_status=unsubscribed",
        ),
        ("verified", "Verified", Q(verified_at__isnull=False), "verified=verified"),
        ("unverified", "Unverified", Q(verified_at__isnull=True), "verified=unverified"),
        ("inactive", "Inactive", Q(Exists(sent)) & ~opened & ~clicked, None),
        (
            "global_unsubscribed",
            "Global unsubscribed",
            Q(global_unsubscribed_at__isnull=False),
            "suppression=global_unsubscribed",
        ),
        ("hard_bounced", "Hard bounced", Q(hard_bounced_at__isnull=False), "suppression=hard_bounced"),
        ("complained", "Complained", Q(complained_at__isnull=False), "suppression=complained"),
        ("opened", "Opened", opened, None),
        ("clicked", "Clicked", clicked, None),
    ]
    counts = contacts.aggregate(
        **{key: Count("pk", filter=predicate) for key, _label, predicate, _query in definitions}
    )
    return [
        Stat(
            key,
            label,
            counts[key],
            href=("?section=members" + ("&" + query if query else "")) if query is not None else "",
        )
        for key, label, _predicate, query in definitions
    ]


def scoped_member_contacts(audience, client):
    membership = Subscription.objects.filter(contact_id=OuterRef("pk"), audience=audience, client=client)
    return Contact.objects.filter(Exists(membership))


def scoped_tags(audience, client):
    contacts = scoped_member_contacts(audience, client)
    return (
        Tag.objects.filter(audience=audience)
        .annotate(count=Count("contact_tags", filter=Q(contact_tags__contact__in=contacts), distinct=True))
        .order_by("slug")
    )


def scoped_breakdowns(audience, client):
    result = {}
    contacts = scoped_member_contacts(audience, client)
    result["validation"] = count_by_field(
        contacts,
        "email_validation_status",
        choices=[(value, email_validation_label(value)) for value, _label in EmailValidationStatus.choices],
    )
    result["tags"] = scoped_tags(audience, client)
    recipients = CampaignRecipient.objects.filter(
        campaign__audience=audience, campaign__client=client, contact__in=contacts
    )
    for field, choices in [
        ("status", CampaignRecipientStatus.choices),
        ("skip_reason", CampaignRecipientSkipReason.choices),
    ]:
        counts = {
            row[field]: row["count"]
            for row in recipients.exclude(**{field: ""})
            .values(field)
            .annotate(count=Count("contact_id", distinct=True))
        }
        result["campaign_statuses" if field == "status" else "skip_reasons"] = [
            (label, counts[value]) for value, label in choices if counts.get(value)
        ]
    return result


def segment_links(breakdowns):
    groups = {}
    for field, parameter, values in [
        (
            "validation",
            "email_validation_status",
            [(email_validation_label(value), value) for value, _label in EmailValidationStatus.choices],
        ),
        ("campaign_statuses", "campaign_status", [(label, value) for value, label in CampaignRecipientStatus.choices]),
        ("skip_reasons", "skip_reason", [(label, value) for value, label in CampaignRecipientSkipReason.choices]),
    ]:
        lookup = dict(values)
        groups[field] = [
            {"label": label, "count": count, "url": f"?section=members&{parameter}={lookup[label]}"}
            for label, count in breakdowns[field]
        ]
    return groups


def audience_page_context(request, audience, active_client):
    section = request.GET.get("section", "members")
    if section not in dict(SECTIONS):
        section = "members"
    # Existing bookmarked event/campaign pagination links predate section routing.
    if "section" not in request.GET and request.GET.get("event_type"):
        section = "activity"
    filters = parse_contact_explorer_filters(
        request.GET, forced_audience_id=audience.id, forced_client_id=active_client.id
    )
    base = reverse("mailing:audience_detail", args=[audience.id])
    counts, eligible_ids, excluded_ids = eligibility_summary(audience, active_client)
    count_links = []
    for key, title in [
        ("total", "Total members"),
        ("eligible", "Eligible for marketing"),
        ("excluded", "Excluded from marketing"),
    ]:
        params = request.GET.copy()
        for field in [*FILTER_KEYS.values(), "page", "event_type", "eligibility", "inactive_since"]:
            params.pop(field, None)
        params["section"] = "members"
        if key != "total":
            params["eligibility"] = key
        count_links.append({"label": title, "value": counts[key], "url": base + "?" + params.urlencode()})
    params = request.GET.copy()
    params.pop("page", None)
    params["section"] = section
    context = {
        "audience": audience,
        "active_client": active_client,
        "section": section,
        "sections": section_links(request, base, section),
        "eligibility_summary": count_links,
        "filters": filters,
        "filter_chips": removable_filters(request, filters, base),
        "pagination_querystring": params.urlencode(),
    }
    if section == "members":
        queryset = contact_explorer_queryset(filters)
        eligibility = request.GET.get("eligibility")
        if eligibility in {"eligible", "excluded"}:
            queryset = queryset.filter(pk__in=eligible_ids if eligibility == "eligible" else excluded_ids)
        members = Paginator(queryset, 25).get_page(request.GET.get("page"))
        context.update(
            {
                "members": members,
                "member_rows": contact_result_rows(members.object_list, audience=audience, client=active_client),
                "options": contact_explorer_options(active_client),
                "breakdowns": {"tags": scoped_tags(audience, active_client)},
                "eligibility": eligibility,
            }
        )
    elif section == "segments":
        context["breakdowns"] = scoped_breakdowns(audience, active_client)
        context["segment_links"] = segment_links(context["breakdowns"])
    elif section == "campaigns":
        context["campaigns"] = Paginator(audience_campaign_history(audience, active_client), 10).get_page(
            request.GET.get("page")
        )
    elif section == "activity":
        event_type = request.GET.get("event_type", "")
        events = Paginator(audience_recent_events(audience, event_type, active_client), 25).get_page(
            request.GET.get("page")
        )
        context.update(
            {
                "events": events,
                "event_type": event_type,
                "event_type_options": choices_from_text_choices(EmailEventType),
                "event_rows": [
                    {
                        "event": event,
                        "context": event_context(event),
                        "metadata_summary": metadata_summary(event.metadata),
                    }
                    for event in events
                ],
            }
        )
        context["activity_hidden_filters"] = [
            (key, value)
            for key, values in request.GET.lists()
            if key not in {"event_type", "page", "section"}
            for value in values
        ]
    elif section == "health":
        context.update(
            {"summary": scoped_health_summary(audience, active_client), "audit_rows": latest_audits_for(audience)}
        )
    return context
