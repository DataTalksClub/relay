from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time

from django.db.models import Case, Count, Exists, IntegerField, Max, Min, OuterRef, Q, QuerySet, Subquery, When
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_date

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientSkipReason,
    CampaignRecipientStatus,
    CampaignStatus,
    Client,
    Contact,
    ContactTag,
    EmailEvent,
    EmailEventType,
    EmailValidationStatus,
    Subscription,
    SubscriptionStatus,
    Tag,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.contacts import has_invalid_email_validation, normalize_email
from mailing.services.worker_status import WorkerStatus, sandbox_worker_statuses

RECIPIENT_FILTERS = {
    "delivered": Q(delivered_at__isnull=False),
    "opened": Q(first_opened_at__isnull=False),
    "clicked": Q(first_clicked_at__isnull=False),
    "not_opened": Q(first_opened_at__isnull=True),
    "bounced": Q(status=CampaignRecipientStatus.BOUNCED),
    "unsubscribed": Q(status=CampaignRecipientStatus.UNSUBSCRIBED),
    "complained": Q(status=CampaignRecipientStatus.COMPLAINED),
    "skipped": Q(status=CampaignRecipientStatus.SKIPPED),
    "failed": Q(status=CampaignRecipientStatus.FAILED),
    "sent": Q(status=CampaignRecipientStatus.SENT),
    "pending": Q(status=CampaignRecipientStatus.PENDING),
}

PROCESSED_RECIPIENT_STATUSES = {
    CampaignRecipientStatus.SENT,
    CampaignRecipientStatus.FAILED,
    CampaignRecipientStatus.BOUNCED,
    CampaignRecipientStatus.COMPLAINED,
    CampaignRecipientStatus.UNSUBSCRIBED,
}

RECIPIENT_FILTER_LABELS = {
    "delivered": "Delivered",
    "opened": "Opened",
    "clicked": "Clicked",
    "not_opened": "Not opened",
    "bounced": "Bounced",
    "unsubscribed": "Unsubscribed",
    "complained": "Complained",
    "skipped": "Skipped",
    "failed": "Failed",
    "sent": "Sent",
    "pending": "Pending",
}

ENGAGEMENT_FILTER_LABELS = {
    "not_opened": "Not opened",
    "not_clicked": "Not clicked",
    "opened_not_clicked": "Opened, not clicked",
    "never_opened": "Never opened",
    "never_clicked": "Never clicked",
    "inactive_since": "Inactive since",
}

SUPPRESSION_FILTER_LABELS = {
    "global_unsubscribed": "Global unsubscribed",
    "hard_bounced": "Hard bounced",
    "complained": "Complained",
}

VERIFIED_FILTER_LABELS = {
    "verified": "Verified",
    "unverified": "Unverified",
}


@dataclass(frozen=True)
class Stat:
    key: str
    label: str
    value: int
    rate: str = ""
    href: str = ""
    # Count strips carry a colored dot when the count is itself a state to
    # act on (the family count-strip convention); empty means a neutral dot.
    tone: str = ""


@dataclass(frozen=True)
class Choice:
    value: str
    label: str


@dataclass(frozen=True)
class ContactExplorerFilters:
    query: str = ""
    audience_id: int | None = None
    client_id: int | None = None
    include_tags: tuple[str, ...] = ()
    exclude_tags: tuple[str, ...] = ()
    subscription_status: str = ""
    verified_state: str = ""
    email_validation_status: str = ""
    suppression_state: str = ""
    campaign_status: str = ""
    skip_reason: str = ""
    engagement: str = ""
    inactive_since: object | None = None

    @property
    def has_filters(self):
        return any(
            (
                self.query,
                self.audience_id,
                self.client_id,
                self.include_tags,
                self.exclude_tags,
                self.subscription_status,
                self.verified_state,
                self.email_validation_status,
                self.suppression_state,
                self.campaign_status,
                self.skip_reason,
                self.engagement,
            )
        )


@dataclass(frozen=True)
class ContactResultRow:
    contact: Contact
    verification_badge: "Badge"
    validation_badge: "Badge"
    subscription_badge: "Badge"
    subscription_summary: str
    tag_summary: str
    last_sent_at: object | None
    last_opened_at: object | None
    last_clicked_at: object | None
    recent_issue: str
    eligibility_badge: "Badge" | None = None
    eligibility_reasons: tuple[str, ...] = ()

    @property
    def primary_status_badge(self) -> "Badge":
        return self.eligibility_badge or self.subscription_badge

    @property
    def hidden_status_badges(self) -> tuple["Badge", ...]:
        return (self.subscription_badge, self.validation_badge, self.verification_badge)

    @property
    def hidden_status_badge_count(self) -> int:
        return len(self.hidden_status_badges)

    @property
    def hidden_status_badge_summary(self) -> str:
        return ", ".join(badge.label for badge in self.hidden_status_badges)


@dataclass(frozen=True)
class ActiveFilter:
    label: str
    value: str


@dataclass(frozen=True)
class EligibilityItem:
    scope: str
    can_send_marketing: bool
    marketing_reasons: tuple[str, ...]
    can_send_transactional: bool
    transactional_reasons: tuple[str, ...]


@dataclass(frozen=True)
class Badge:
    label: str
    tone: str = "neutral"


@dataclass(frozen=True)
class CampaignListRow:
    campaign: Campaign
    badge: Badge
    timing_label: str
    timing_value: object | None
    progress: "CampaignSendProgress | None" = None


@dataclass(frozen=True)
class CampaignSendProgress:
    queued_count: int
    processed_count: int
    sent_count: int
    started_at: object | None
    ended_at: object | None
    duration_seconds: int | None
    duration_label: str
    per_second: str
    per_minute: str

    @property
    def has_started(self) -> bool:
        return self.started_at is not None


@dataclass(frozen=True)
class AudienceListRow:
    audience: Audience
    member_count: int
    subscribed_count: int
    inactive_count: int
    suppressed_count: int
    campaign_count: int
    recent_campaign: Campaign | None
    recent_event: EmailEvent | None


@dataclass(frozen=True)
class ContactMetric:
    label: str
    value: object | None
    tone: str = "neutral"


@dataclass(frozen=True)
class SendabilitySummary:
    marketing_badge: Badge
    marketing_reasons: tuple[str, ...]
    transactional_badge: Badge
    transactional_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ContactActivity:
    occurred_at: object | None
    badge: Badge
    title: str
    detail: str
    href: str = ""
    metadata: str = ""


@dataclass(frozen=True)
class ContactDetailContext:
    eligibility: list[EligibilityItem]
    subscriptions: QuerySet[Subscription]
    contact_tags: QuerySet[ContactTag]
    verification_badge: Badge
    validation_badge: Badge
    subscription_badge: Badge
    sendability: SendabilitySummary
    metrics: list[ContactMetric]
    recent_activity: list[ContactActivity]


@dataclass(frozen=True)
class DashboardCampaign:
    campaign: Campaign
    badge: Badge


@dataclass(frozen=True)
class DashboardAttentionItem:
    occurred_at: object | None
    badge: Badge
    title: str
    detail: str
    context: str = ""
    href: str = ""


@dataclass(frozen=True)
class DashboardClient:
    client: Client
    active_api_key_count: int


@dataclass(frozen=True)
class DashboardContext:
    recent_campaigns: list[DashboardCampaign]
    attention_items: list[DashboardAttentionItem]
    worker_statuses: list[WorkerStatus]
    worker_health_label: str
    worker_health_tone: str
    worker_waiting_count: int
    integration_clients: list[DashboardClient]
    transactional_backlog_count: int


def rate(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return ""
    return f"{(numerator / denominator) * 100:.1f}%"


def choices_from_text_choices(text_choices) -> list[Choice]:
    return [Choice(value=value, label=label) for value, label in text_choices.choices]


def dashboard_context(client: Client | None = None) -> DashboardContext:
    worker_statuses = sandbox_worker_statuses()
    worker_health_label, worker_health_tone = worker_health(worker_statuses)
    worker_waiting_count = sum(worker.backlog_count or 0 for worker in worker_statuses)
    campaign_scope = Campaign.objects.all()
    client_scope = Client.objects.all()
    if client is not None:
        campaign_scope = campaign_scope.filter(client=client)
        client_scope = client_scope.filter(pk=client.pk)

    if client is not None:
        transactional_backlog_count = TransactionalMessage.objects.filter(
            status=TransactionalMessageStatus.QUEUED, client=client
        ).count()
    else:
        tx_worker = next((w for w in worker_statuses if w.key == "transactional"), None)
        transactional_backlog_count = (
            tx_worker.backlog_count
            if tx_worker is not None and tx_worker.backlog_count is not None
            else TransactionalMessage.objects.filter(status=TransactionalMessageStatus.QUEUED).count()
        )

    recent_campaigns = [
        DashboardCampaign(campaign, campaign_status_badge(campaign.status))
        for campaign in campaign_scope.select_related("client", "audience")
        .annotate(work_priority=Case(
            When(status__in=[CampaignStatus.SCHEDULED, CampaignStatus.QUEUED, CampaignStatus.SNAPSHOTTING, CampaignStatus.SENDING], then=0),
            When(status=CampaignStatus.DRAFT, then=1), default=2, output_field=IntegerField(),
        ))
        .order_by("work_priority", "-updated_at", "-id")[:5]
    ]
    integration_clients = [
        DashboardClient(
            scope_client,
            scope_client.active_key_count
            if hasattr(scope_client, "active_key_count")
            else scope_client.active_api_key_count,
        )
        for scope_client in client_scope.select_related("organization")
        .annotate(active_key_count=Count("api_keys", filter=Q(api_keys__revoked_at__isnull=True)))
        .order_by("organization__slug", "slug")[:4]
    ]

    return DashboardContext(
        recent_campaigns=recent_campaigns,
        attention_items=dashboard_attention_items(client),
        worker_statuses=worker_statuses,
        worker_health_label=worker_health_label,
        worker_health_tone=worker_health_tone,
        worker_waiting_count=worker_waiting_count,
        integration_clients=integration_clients,
        transactional_backlog_count=transactional_backlog_count,
    )


def worker_health(workers: list[WorkerStatus]) -> tuple[str, str]:
    if any(worker.badge_tone in {"danger", "warning"} for worker in workers):
        return "Degraded", "danger"
    if not workers or any(worker.alive is None for worker in workers):
        return "Processing health unavailable", "neutral"
    return "Healthy", "success"


def dashboard_attention_items(client: Client | None = None) -> list[DashboardAttentionItem]:
    event_queryset = EmailEvent.objects.select_related("contact", "client", "audience", "campaign")
    if client is not None:
        event_queryset = event_queryset.filter(client=client)
    next_steps = {
        EmailEventType.BOUNCE: "Review the delivery reason and confirm the address before sending again.",
        EmailEventType.COMPLAINT: "Keep this contact suppressed; review consent and the campaign audience.",
        EmailEventType.FAILED: "Review the failure reason and sending configuration before retrying.",
    }
    return [
        DashboardAttentionItem(
            event.created_at,
            Badge(event.get_event_type_display(), event_tone(event.event_type)),
            event.contact.email if event.contact_id else event_context(event),
            f"{metadata_summary(event.metadata) or event.url or event.get_event_type_display()}. "
            f"{next_steps[event.event_type]}",
            context=event_context(event),
            href=f"/contacts/{event.contact.normalized_email}/" if event.contact_id else "",
        )
        for event in event_queryset.filter(event_type__in=next_steps).order_by("-created_at", "-id")[:5]
    ]


def campaign_status_badge(status: str) -> Badge:
    label = CampaignStatus(status).label
    if status == CampaignStatus.SNAPSHOTTING:
        label = "Preparing recipients"
    if status == CampaignStatus.SENT:
        return Badge(label, "success")
    if status in {CampaignStatus.SCHEDULED, CampaignStatus.QUEUED, CampaignStatus.SNAPSHOTTING, CampaignStatus.SENDING}:
        return Badge(label, "warning")
    if status in {CampaignStatus.FAILED, CampaignStatus.CANCELLED}:
        return Badge(label, "danger")
    return Badge(label, "neutral")


def campaign_timing(campaign: Campaign) -> tuple[str, object | None]:
    if campaign.sent_at:
        return "Sent", campaign.sent_at
    if campaign.status == CampaignStatus.SCHEDULED and campaign.scheduled_at:
        return "Scheduled", campaign.scheduled_at
    if campaign.status in {CampaignStatus.QUEUED, CampaignStatus.SNAPSHOTTING, CampaignStatus.SENDING}:
        return "Queued", campaign.updated_at
    return "Updated", campaign.updated_at


def campaign_list_rows(campaigns, *, include_progress: bool = False) -> list[CampaignListRow]:
    campaigns = list(campaigns)
    progress_by_id = campaign_progress_map([campaign.pk for campaign in campaigns]) if include_progress else {}
    rows = []
    for campaign in campaigns:
        timing_label, timing_value = campaign_timing(campaign)
        progress = (progress_by_id.get(campaign.pk) or campaign_send_progress(campaign)) if include_progress else None
        rows.append(
            CampaignListRow(
                campaign=campaign,
                badge=campaign_status_badge(campaign.status),
                timing_label=timing_label,
                timing_value=timing_value,
                progress=progress,
            )
        )
    return rows


def _progress_from_row(row) -> CampaignSendProgress:
    sent_count = row["sent_count"] or 0
    started_at = row["started_at"]
    ended_at = row["ended_at"]
    duration_seconds = None
    per_second = ""
    per_minute = ""
    if started_at and ended_at and sent_count:
        duration_seconds = max(int((ended_at - started_at).total_seconds()), 1)
        per_second = f"{sent_count / duration_seconds:.2f}/sec"
        per_minute = f"{(sent_count / duration_seconds) * 60:.1f}/min"

    return CampaignSendProgress(
        queued_count=row["queued_count"] or 0,
        processed_count=row["processed_count"] or 0,
        sent_count=sent_count,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration_seconds,
        duration_label=format_duration(duration_seconds),
        per_second=per_second,
        per_minute=per_minute,
    )


def campaign_progress_map(campaign_ids) -> dict:
    """One grouped query for every campaign on the page (was 3 queries/row)."""
    ids = [pk for pk in dict.fromkeys(campaign_ids) if pk is not None]
    if not ids:
        return {}
    rows = (
        CampaignRecipient.objects.filter(campaign_id__in=ids)
        .order_by()
        .values("campaign_id")
        .annotate(
            queued_count=Count("id", filter=Q(status=CampaignRecipientStatus.PENDING)),
            processed_count=Count("id", filter=Q(status__in=PROCESSED_RECIPIENT_STATUSES)),
            sent_count=Count("id", filter=Q(sent_at__isnull=False)),
            started_at=Min("sent_at"),
            ended_at=Max("sent_at"),
        )
    )
    return {row["campaign_id"]: _progress_from_row(row) for row in rows}


def campaign_send_progress(campaign: Campaign) -> CampaignSendProgress:
    row = (
        CampaignRecipient.objects.filter(campaign=campaign)
        .order_by()
        .aggregate(
            queued_count=Count("id", filter=Q(status=CampaignRecipientStatus.PENDING)),
            processed_count=Count("id", filter=Q(status__in=PROCESSED_RECIPIENT_STATUSES)),
            sent_count=Count("id", filter=Q(sent_at__isnull=False)),
            started_at=Min("sent_at"),
            ended_at=Max("sent_at"),
        )
    )
    return _progress_from_row(row)


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    minutes, remaining_seconds = divmod(seconds, 60)
    hours, remaining_minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if remaining_minutes:
        parts.append(f"{remaining_minutes}m")
    if remaining_seconds or not parts:
        parts.append(f"{remaining_seconds}s")
    return " ".join(parts)


def campaign_stats(campaign: Campaign, progress: CampaignSendProgress | None = None) -> list[Stat]:
    failed_count = CampaignRecipient.objects.filter(
        campaign=campaign,
        status=CampaignRecipientStatus.FAILED,
    ).count()
    recipient_count = campaign.recipient_count
    sent_count = campaign.sent_count
    progress = progress or campaign_send_progress(campaign)

    return [
        Stat("recipients", "Recipients", recipient_count),
        Stat("queued", "Queued", progress.queued_count),
        Stat("processed", "Processed", progress.processed_count, rate(progress.processed_count, recipient_count)),
        Stat("sent", "Sent", sent_count, rate(sent_count, recipient_count)),
        Stat("skipped", "Skipped", campaign.skipped_count, rate(campaign.skipped_count, recipient_count)),
        Stat("delivered", "Delivered", campaign.delivered_count, rate(campaign.delivered_count, recipient_count)),
        Stat("unique_opens", "Unique opens", campaign.unique_open_count, rate(campaign.unique_open_count, recipient_count)),
        Stat("total_opens", "Total opens", campaign.open_count),
        Stat(
            "unique_clicks",
            "Unique clicks",
            campaign.unique_click_count,
            rate(campaign.unique_click_count, recipient_count),
        ),
        Stat("total_clicks", "Total clicks", campaign.click_count),
        Stat("unsubscribes", "Unsubscribes", campaign.unsubscribe_count, rate(campaign.unsubscribe_count, recipient_count)),
        Stat("bounces", "Bounces", campaign.bounce_count, rate(campaign.bounce_count, recipient_count)),
        Stat("complaints", "Complaints", campaign.complaint_count, rate(campaign.complaint_count, recipient_count)),
        Stat("failures", "Failures", failed_count, rate(failed_count, recipient_count)),
    ]


def campaign_stat_groups(campaign: Campaign, progress: CampaignSendProgress | None = None) -> dict[str, list[Stat]]:
    stats_by_key = {stat.key: stat for stat in campaign_stats(campaign, progress)}
    return {
        "delivery": [stats_by_key[key] for key in ("sent", "delivered", "bounces", "complaints", "failures")],
        "engagement": [stats_by_key[key] for key in ("unique_opens", "unique_clicks", "unsubscribes")],
        "progress": [stats_by_key[key] for key in ("queued", "processed")],
    }


def campaign_queryset(client: Client | None = None) -> QuerySet[Campaign]:
    queryset = Campaign.objects.select_related("client", "audience", "audience__organization")
    if client is not None:
        queryset = queryset.filter(client=client)
    return queryset.order_by("-created_at", "-id")


def campaign_recent_events(campaign: Campaign) -> QuerySet[EmailEvent]:
    return (
        EmailEvent.objects.filter(campaign=campaign)
        .select_related("contact", "client", "audience", "campaign_recipient")
        .order_by("-created_at", "-id")
    )


def campaign_recipient_queryset(campaign: Campaign, filter_key: str = "") -> QuerySet[CampaignRecipient]:
    queryset = (
        CampaignRecipient.objects.filter(campaign=campaign)
        .select_related("contact")
        .order_by("contact__normalized_email", "id")
    )
    filter_condition = RECIPIENT_FILTERS.get(filter_key)
    if filter_condition is not None:
        queryset = queryset.filter(filter_condition)
    return queryset


def contact_search_queryset(query: str) -> QuerySet[Contact]:
    filters = ContactExplorerFilters(query=query)
    if not filters.query.strip():
        return Contact.objects.none()
    return contact_explorer_queryset(filters)


def contact_explorer_options(client: Client | None = None):
    audience_queryset = Audience.objects.select_related("organization")
    client_queryset = Client.objects.select_related("organization")
    tag_queryset = Tag.objects.select_related("audience")
    if client is not None:
        audience_queryset = audience_queryset.filter(organization=client.organization)
        client_queryset = client_queryset.filter(pk=client.pk)
        tag_queryset = tag_queryset.filter(audience__organization=client.organization)
    return {
        "audiences": audience_queryset.order_by("organization__slug", "slug"),
        "clients": client_queryset.order_by("organization__slug", "slug"),
        "tags": tag_queryset.order_by("audience__slug", "slug"),
        "subscription_statuses": choices_from_text_choices(SubscriptionStatus),
        "verified_states": [Choice(value=key, label=label) for key, label in VERIFIED_FILTER_LABELS.items()],
        "email_validation_statuses": [
            Choice(value=value, label=email_validation_label(value)) for value, _label in EmailValidationStatus.choices
        ],
        "suppression_states": [Choice(value=key, label=label) for key, label in SUPPRESSION_FILTER_LABELS.items()],
        "campaign_statuses": choices_from_text_choices(CampaignRecipientStatus),
        "skip_reasons": choices_from_text_choices(CampaignRecipientSkipReason),
        "engagement_states": [Choice(value=key, label=label) for key, label in ENGAGEMENT_FILTER_LABELS.items()],
    }


def active_contact_filters(filters: ContactExplorerFilters) -> list[ActiveFilter]:
    chips = []
    if filters.query:
        chips.append(ActiveFilter("Email", filters.query))
    if filters.audience_id:
        audience = Audience.objects.filter(pk=filters.audience_id).first()
        chips.append(ActiveFilter("Audience", audience.name if audience else str(filters.audience_id)))
    if filters.client_id:
        client = Client.objects.filter(pk=filters.client_id).first()
        chips.append(ActiveFilter("Client", client.name if client else str(filters.client_id)))
    if filters.subscription_status:
        chips.append(ActiveFilter("Subscription", SubscriptionStatus(filters.subscription_status).label))
    if filters.verified_state:
        chips.append(ActiveFilter("Verification", VERIFIED_FILTER_LABELS[filters.verified_state]))
    if filters.email_validation_status:
        chips.append(ActiveFilter("Validation", email_validation_label(filters.email_validation_status)))
    if filters.suppression_state:
        chips.append(ActiveFilter("Suppression", SUPPRESSION_FILTER_LABELS[filters.suppression_state]))
    if filters.campaign_status:
        chips.append(ActiveFilter("Campaign", CampaignRecipientStatus(filters.campaign_status).label))
    if filters.skip_reason:
        chips.append(ActiveFilter("Skip reason", CampaignRecipientSkipReason(filters.skip_reason).label))
    if filters.engagement:
        value = ENGAGEMENT_FILTER_LABELS[filters.engagement]
        if filters.engagement == "inactive_since" and filters.inactive_since:
            value = f"{value} {filters.inactive_since.isoformat()}"
        chips.append(ActiveFilter("Engagement", value))
    chips.extend(ActiveFilter("Includes tag", tag) for tag in filters.include_tags)
    chips.extend(ActiveFilter("Excludes tag", tag) for tag in filters.exclude_tags)
    return chips


def parse_contact_explorer_filters(params, *, forced_audience_id=None, forced_client_id=None) -> ContactExplorerFilters:
    inactive_since = parse_date(params.get("inactive_since", ""))
    engagement = params.get("engagement", "")
    if engagement == "inactive_since" and inactive_since is None:
        engagement = ""

    return ContactExplorerFilters(
        query=params.get("q", "").strip(),
        audience_id=forced_audience_id or positive_int(params.get("audience")),
        client_id=forced_client_id or positive_int(params.get("client")),
        include_tags=tuple(nonempty_values(params.getlist("include_tags"))),
        exclude_tags=tuple(nonempty_values(params.getlist("exclude_tags"))),
        subscription_status=valid_choice(params.get("subscription_status", ""), SubscriptionStatus),
        verified_state=params.get("verified", "") if params.get("verified", "") in VERIFIED_FILTER_LABELS else "",
        email_validation_status=valid_choice(params.get("email_validation_status", ""), EmailValidationStatus),
        suppression_state=params.get("suppression", "")
        if params.get("suppression", "") in SUPPRESSION_FILTER_LABELS
        else "",
        campaign_status=valid_choice(params.get("campaign_status", ""), CampaignRecipientStatus),
        skip_reason=valid_choice(params.get("skip_reason", ""), CampaignRecipientSkipReason),
        engagement=engagement if engagement in ENGAGEMENT_FILTER_LABELS else "",
        inactive_since=inactive_since,
    )


def positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def valid_choice(value, text_choices) -> str:
    values = {choice.value for choice in text_choices}
    return value if value in values else ""


def nonempty_values(values):
    return [value for value in values if str(value).strip()]


def contact_explorer_queryset(filters: ContactExplorerFilters) -> QuerySet[Contact]:
    queryset = Contact.objects.all()

    if filters.query:
        normalized = normalize_email(filters.query)
        queryset = queryset.filter(Q(normalized_email__icontains=normalized) | Q(email__icontains=filters.query))
    subscription_scope = subscription_scope_queryset(filters)
    if filters.audience_id or filters.client_id or filters.subscription_status:
        queryset = queryset.filter(Exists(subscription_scope))
    if filters.verified_state == "verified":
        queryset = queryset.filter(verified_at__isnull=False)
    elif filters.verified_state == "unverified":
        queryset = queryset.filter(verified_at__isnull=True)
    if filters.email_validation_status:
        queryset = queryset.filter(email_validation_status=filters.email_validation_status)
    if filters.suppression_state == "global_unsubscribed":
        queryset = queryset.filter(global_unsubscribed_at__isnull=False)
    elif filters.suppression_state == "hard_bounced":
        queryset = queryset.filter(hard_bounced_at__isnull=False)
    elif filters.suppression_state == "complained":
        queryset = queryset.filter(complained_at__isnull=False)
    for tag_slug in filters.include_tags:
        queryset = queryset.filter(Exists(contact_tag_scope_queryset(filters).filter(tag__slug=tag_slug)))
    for tag_slug in filters.exclude_tags:
        queryset = queryset.exclude(Exists(contact_tag_scope_queryset(filters).filter(tag__slug=tag_slug)))
    if filters.campaign_status or filters.skip_reason:
        matching_recipients = CampaignRecipient.objects.filter(**campaign_scope_filter(filters))
        if filters.campaign_status:
            matching_recipients = matching_recipients.filter(status=filters.campaign_status)
        if filters.skip_reason:
            matching_recipients = matching_recipients.filter(skip_reason=filters.skip_reason)
        queryset = queryset.filter(Exists(matching_recipients))

    queryset = apply_engagement_filter(queryset, filters)
    recipient_activity = CampaignRecipient.objects.filter(**campaign_scope_filter(filters)).order_by().values("contact_id")
    transactional_activity = (
        TransactionalMessage.objects.filter(**transactional_scope_filter(filters)).order_by().values("contact_id")
    )

    return (
        queryset.prefetch_related(
            "subscriptions__audience",
            "subscriptions__client",
            "contact_tags__tag__audience",
        )
        .annotate(
            campaign_recipient_count=Coalesce(
                Subquery(recipient_activity.annotate(c=Count("id")).values("c")[:1], output_field=IntegerField()),
                0,
            ),
            transactional_message_count=Coalesce(
                Subquery(transactional_activity.annotate(c=Count("id")).values("c")[:1], output_field=IntegerField()),
                0,
            ),
            last_campaign_sent_at=Subquery(recipient_activity.annotate(m=Max("sent_at")).values("m")[:1]),
            last_transactional_sent_at=Subquery(transactional_activity.annotate(m=Max("sent_at")).values("m")[:1]),
            last_campaign_opened_at=Subquery(recipient_activity.annotate(m=Max("first_opened_at")).values("m")[:1]),
            last_transactional_opened_at=Subquery(
                transactional_activity.annotate(m=Max("first_opened_at")).values("m")[:1]
            ),
            last_campaign_clicked_at=Subquery(recipient_activity.annotate(m=Max("first_clicked_at")).values("m")[:1]),
            last_transactional_clicked_at=Subquery(
                transactional_activity.annotate(m=Max("first_clicked_at")).values("m")[:1]
            ),
        )
        .order_by("normalized_email", "id")
    )


def subscription_scope_queryset(filters: ContactExplorerFilters):
    scope = {"contact_id": OuterRef("pk")}
    if filters.audience_id:
        scope["audience_id"] = filters.audience_id
    if filters.client_id:
        scope["client_id"] = filters.client_id
    if filters.subscription_status:
        scope["status"] = filters.subscription_status
    return Subscription.objects.filter(**scope)


def contact_tag_scope_queryset(filters: ContactExplorerFilters):
    scope = {"contact_id": OuterRef("pk")}
    if filters.audience_id:
        scope["tag__audience_id"] = filters.audience_id
    return ContactTag.objects.filter(**scope)


def apply_engagement_filter(queryset: QuerySet[Contact], filters: ContactExplorerFilters) -> QuerySet[Contact]:
    if not filters.engagement:
        return queryset

    campaign_sent = campaign_sent_queryset(filters)
    campaign_open = campaign_open_queryset(filters)
    campaign_click = campaign_click_queryset(filters)
    tx_sent = transactional_sent_queryset(filters)
    tx_open = transactional_open_queryset(filters)
    tx_click = transactional_click_queryset(filters)

    if filters.engagement == "not_opened":
        return queryset.filter(Exists(campaign_sent.filter(first_opened_at__isnull=True)))
    if filters.engagement == "not_clicked":
        return queryset.filter(Exists(campaign_sent.filter(first_clicked_at__isnull=True)))
    if filters.engagement == "opened_not_clicked":
        return queryset.filter(Exists(campaign_open)).exclude(Exists(campaign_click))
    if filters.engagement == "never_opened":
        return queryset.filter(Q(Exists(campaign_sent)) | Q(Exists(tx_sent))).exclude(
            Q(Exists(campaign_open)) | Q(Exists(tx_open))
        )
    if filters.engagement == "never_clicked":
        return queryset.filter(Q(Exists(campaign_sent)) | Q(Exists(tx_sent))).exclude(
            Q(Exists(campaign_click)) | Q(Exists(tx_click))
        )
    if filters.engagement == "inactive_since" and filters.inactive_since:
        cutoff = timezone.make_aware(datetime.combine(filters.inactive_since, time.min))
        return queryset.filter(
            Q(Exists(campaign_sent.filter(sent_at__lt=cutoff))) | Q(Exists(tx_sent.filter(sent_at__lt=cutoff)))
        ).exclude(
            Q(Exists(campaign_open.filter(first_opened_at__gte=cutoff)))
            | Q(Exists(campaign_click.filter(first_clicked_at__gte=cutoff)))
            | Q(Exists(tx_open.filter(first_opened_at__gte=cutoff)))
            | Q(Exists(tx_click.filter(first_clicked_at__gte=cutoff)))
        )
    return queryset


def campaign_scope_filter(filters: ContactExplorerFilters):
    scope = {"contact_id": OuterRef("pk")}
    if filters.audience_id:
        scope["campaign__audience_id"] = filters.audience_id
    if filters.client_id:
        scope["campaign__client_id"] = filters.client_id
    return scope


def transactional_scope_filter(filters: ContactExplorerFilters):
    scope = {"contact_id": OuterRef("pk")}
    if filters.client_id:
        scope["client_id"] = filters.client_id
    return scope


def campaign_sent_queryset(filters: ContactExplorerFilters):
    return CampaignRecipient.objects.filter(**campaign_scope_filter(filters)).filter(
        Q(status=CampaignRecipientStatus.SENT) | Q(delivered_at__isnull=False) | Q(sent_at__isnull=False)
    )


def campaign_open_queryset(filters: ContactExplorerFilters):
    return CampaignRecipient.objects.filter(**campaign_scope_filter(filters), first_opened_at__isnull=False)


def campaign_click_queryset(filters: ContactExplorerFilters):
    return CampaignRecipient.objects.filter(**campaign_scope_filter(filters), first_clicked_at__isnull=False)


def transactional_sent_queryset(filters: ContactExplorerFilters):
    return TransactionalMessage.objects.filter(**transactional_scope_filter(filters)).filter(
        Q(status=TransactionalMessageStatus.SENT) | Q(delivered_at__isnull=False) | Q(sent_at__isnull=False)
    )


def transactional_open_queryset(filters: ContactExplorerFilters):
    return TransactionalMessage.objects.filter(**transactional_scope_filter(filters), first_opened_at__isnull=False)


def transactional_click_queryset(filters: ContactExplorerFilters):
    return TransactionalMessage.objects.filter(**transactional_scope_filter(filters), first_clicked_at__isnull=False)


def contact_result_rows(
    contacts, *, audience: Audience | None = None, client: Client | None = None
) -> list[ContactResultRow]:
    rows = []
    contact_ids = [contact.id for contact in contacts]
    recent_issues = recent_contact_issues(contact_ids, audience=audience, client=client)
    for contact in contacts:
        all_subscriptions = list(contact.subscriptions.all())
        subscriptions = scoped_subscriptions(all_subscriptions, audience, client)
        eligibility = eligibility_items(contact, subscriptions, all_subscriptions=all_subscriptions)
        can_send = any(item.can_send_marketing for item in eligibility)
        reasons = unique_reasons(
            reason for item in eligibility for reason in item.marketing_reasons
            if not can_send and reason != "eligible"
        )
        eligibility_badge = Badge(
            "Can send marketing" if can_send else f"Cannot send — {', '.join(reasons)}",
            "success" if can_send else "warning",
        )
        contact_tags = scoped_contact_tags(contact.contact_tags.all(), audience, client)
        rows.append(
            ContactResultRow(
                contact=contact,
                verification_badge=verification_badge(contact),
                validation_badge=validation_badge(contact),
                subscription_badge=subscription_badge(contact, subscriptions),
                subscription_summary=subscription_summary(subscriptions),
                tag_summary=tag_summary(contact_tags),
                last_sent_at=max_date(contact.last_campaign_sent_at, contact.last_transactional_sent_at),
                last_opened_at=max_date(contact.last_campaign_opened_at, contact.last_transactional_opened_at),
                last_clicked_at=max_date(contact.last_campaign_clicked_at, contact.last_transactional_clicked_at),
                recent_issue=recent_issues.get(contact.id, ""),
                eligibility_badge=eligibility_badge,
                eligibility_reasons=reasons,
            )
        )
    return rows


def scoped_subscriptions(subscriptions, audience: Audience | None, client: Client | None = None):
    return [
        subscription for subscription in subscriptions
        if (audience is None or subscription.audience_id == audience.id)
        and (client is None or subscription.client_id == client.id)
    ]


def scoped_contact_tags(contact_tags, audience: Audience | None, client: Client | None = None):
    return [
        membership for membership in contact_tags
        if (audience is None or membership.tag.audience_id == audience.id)
        and (client is None or membership.tag.audience.organization_id == client.organization_id)
    ]


def max_date(*values):
    present = [value for value in values if value is not None]
    return max(present) if present else None


def subscription_summary(subscriptions) -> str:
    parts = []
    for subscription in subscriptions:
        client_label = subscription.client.slug if subscription.client_id else "audience"
        parts.append(f"{subscription.audience.slug}/{client_label}: {subscription.status}")
    return "; ".join(parts) or "-"


def tag_summary(contact_tags) -> str:
    labels = [f"{membership.tag.audience.slug}/{membership.tag.slug}" for membership in contact_tags]
    return ", ".join(labels) or "-"


def recent_contact_issues(
    contact_ids, *, audience: Audience | None = None, client: Client | None = None
) -> dict[int, str]:
    if not contact_ids:
        return {}
    issue_statuses = [
        CampaignRecipientStatus.FAILED,
        CampaignRecipientStatus.SKIPPED,
        CampaignRecipientStatus.BOUNCED,
        CampaignRecipientStatus.COMPLAINED,
        CampaignRecipientStatus.UNSUBSCRIBED,
    ]
    issues = CampaignRecipient.objects.filter(contact_id__in=contact_ids, status__in=issue_statuses)
    if audience is not None:
        issues = issues.filter(campaign__audience=audience)
    if client is not None:
        issues = issues.filter(campaign__client=client)
    issues = issues.select_related("campaign").order_by("contact_id", "-created_at", "-id")
    result = {}
    for recipient in issues:
        if recipient.contact_id in result:
            continue
        label = recipient.get_skip_reason_display() if recipient.skip_reason else recipient.get_status_display()
        result[recipient.contact_id] = f"{label}: {recipient.campaign.subject}"
    return result


def contact_detail_queryset() -> QuerySet[Contact]:
    return Contact.objects.prefetch_related(
        "subscriptions__audience__organization",
        "subscriptions__client",
        "contact_tags__tag__audience",
    )


def contact_campaign_history(contact: Contact, client: Client | None = None) -> QuerySet[CampaignRecipient]:
    queryset = CampaignRecipient.objects.filter(contact=contact).select_related(
        "campaign",
        "campaign__client",
        "campaign__audience",
    )
    if client is not None:
        queryset = queryset.filter(campaign__client=client)
    return queryset.order_by("-created_at", "-id")


def contact_transactional_history(contact: Contact, client: Client | None = None):
    queryset = contact.transactional_messages.select_related("client", "template")
    if client is not None:
        queryset = queryset.filter(client=client)
    return queryset.order_by("-created_at", "-id").only(
        "id",
        "client__name",
        "client__slug",
        "template_id",
        "template__name",
        "template_key",
        "status",
        "subject",
        "ses_message_id",
        "created_at",
        "sent_at",
        "delivered_at",
        "first_opened_at",
        "first_clicked_at",
        "open_count",
        "click_count",
        "metadata",
        "last_error",
    )


def contact_detail_context(
    contact: Contact,
    client: Client | None = None,
    *,
    events=None,
    campaign_history=None,
    transactional_history=None,
) -> ContactDetailContext:
    all_subscriptions = list(
        contact.subscriptions.select_related("audience", "client").order_by(
            "audience__slug",
            "client__slug",
            "id",
        )
    )
    subscriptions = (
        [sub for sub in all_subscriptions if sub.client_id == client.id]
        if client is not None
        else all_subscriptions
    )
    eligibility = eligibility_items(contact, subscriptions, all_subscriptions=all_subscriptions)
    contact_tags_qs = contact.contact_tags.select_related("tag", "tag__audience").order_by(
        "tag__audience__slug",
        "tag__slug",
    )
    if client is not None:
        contact_tags_qs = contact_tags_qs.filter(tag__audience__organization=client.organization)
    contact_tags = list(contact_tags_qs)
    return ContactDetailContext(
        eligibility=eligibility,
        subscriptions=subscriptions,
        contact_tags=contact_tags,
        verification_badge=verification_badge(contact),
        validation_badge=validation_badge(contact),
        subscription_badge=subscription_badge(contact, subscriptions),
        sendability=sendability_summary(eligibility),
        metrics=contact_metrics(contact, client),
        recent_activity=recent_contact_activity(
            contact,
            client,
            events=events,
            campaign_history=campaign_history,
            transactional_history=transactional_history,
        ),
    )


def verification_badge(contact: Contact) -> Badge:
    if contact.verified_at:
        return Badge("Verified", "success")
    return Badge("Unverified", "warning")


def validation_badge(contact: Contact) -> Badge:
    label = email_validation_label(contact.email_validation_status)
    if contact.email_validation_status in {
        EmailValidationStatus.VALID,
        EmailValidationStatus.EXTERNALLY_VALIDATED,
    }:
        return Badge(label, "success")
    if contact.email_validation_status in {
        EmailValidationStatus.RISKY,
        EmailValidationStatus.UNKNOWN,
    }:
        return Badge(label, "warning" if contact.email_validation_status == EmailValidationStatus.RISKY else "neutral")
    return Badge(label, "danger")


def subscription_badge(contact: Contact, subscriptions) -> Badge:
    if contact.global_unsubscribed_at:
        return Badge("Globally unsubscribed", "danger")
    if contact.complained_at:
        return Badge("Complained", "danger")
    if contact.hard_bounced_at:
        return Badge("Hard bounced", "danger")
    statuses = [subscription.status for subscription in subscriptions]
    if SubscriptionStatus.SUBSCRIBED in statuses:
        return Badge("Subscribed", "success")
    if SubscriptionStatus.UNSUBSCRIBED in statuses:
        return Badge("Unsubscribed", "danger")
    if SubscriptionStatus.PENDING in statuses:
        return Badge("Pending", "warning")
    return Badge("No subscriptions", "neutral")


def sendability_summary(eligibility: list[EligibilityItem]) -> SendabilitySummary:
    marketing_can_send = any(item.can_send_marketing for item in eligibility)
    transactional_can_send = any(item.can_send_transactional for item in eligibility)
    return SendabilitySummary(
        marketing_badge=Badge(
            "Can send marketing" if marketing_can_send else "Cannot send marketing",
            "success" if marketing_can_send else "danger",
        ),
        marketing_reasons=unique_reasons(
            reason
            for item in eligibility
            for reason in item.marketing_reasons
            if marketing_can_send is False or reason != "eligible"
        )
        or ("eligible in at least one subscription scope",),
        transactional_badge=Badge(
            "Can send transactional" if transactional_can_send else "Cannot send transactional",
            "success" if transactional_can_send else "danger",
        ),
        transactional_reasons=unique_reasons(
            reason
            for item in eligibility
            for reason in item.transactional_reasons
            if transactional_can_send is False or reason != "eligible"
        )
        or ("eligible",),
    )


def unique_reasons(reasons) -> tuple[str, ...]:
    seen = []
    for reason in reasons:
        if reason and reason not in seen:
            seen.append(reason)
    return tuple(seen)


def contact_metrics(contact: Contact, client: Client | None = None) -> list[ContactMetric]:
    campaign_rows = CampaignRecipient.objects.filter(contact=contact).order_by()
    transactional_rows = contact.transactional_messages.order_by()
    event_rows = EmailEvent.objects.filter(contact=contact).order_by()
    if client is not None:
        campaign_rows = campaign_rows.filter(campaign__client=client)
        transactional_rows = transactional_rows.filter(client=client)
        event_rows = event_rows.filter(client=client)
    campaign_marks = campaign_rows.aggregate(
        sent=Max("sent_at"),
        opened=Max("first_opened_at"),
        clicked=Max("first_clicked_at"),
    )
    transactional_marks = transactional_rows.aggregate(
        sent=Max("sent_at"),
        opened=Max("first_opened_at"),
        clicked=Max("first_clicked_at"),
    )
    event_marks = event_rows.aggregate(
        bounce=Max("created_at", filter=Q(event_type=EmailEventType.BOUNCE)),
        complaint=Max("created_at", filter=Q(event_type=EmailEventType.COMPLAINT)),
        unsubscribe=Max("created_at", filter=Q(event_type=EmailEventType.UNSUBSCRIBE)),
    )
    return [
        ContactMetric(
            "Last sent",
            latest_datetime(campaign_marks["sent"], transactional_marks["sent"]),
            "success",
        ),
        ContactMetric(
            "Last opened",
            latest_datetime(campaign_marks["opened"], transactional_marks["opened"]),
            "success",
        ),
        ContactMetric(
            "Last clicked",
            latest_datetime(campaign_marks["clicked"], transactional_marks["clicked"]),
            "success",
        ),
        ContactMetric(
            "Last bounce",
            latest_datetime(contact.hard_bounced_at, event_marks["bounce"]),
            "danger",
        ),
        ContactMetric(
            "Last complaint",
            latest_datetime(contact.complained_at, event_marks["complaint"]),
            "danger",
        ),
        ContactMetric(
            "Last unsubscribe",
            latest_datetime(contact.global_unsubscribed_at, event_marks["unsubscribe"]),
            "danger",
        ),
    ]


def latest_datetime(*values):
    present = [value for value in values if value is not None]
    return max(present) if present else None


def recent_contact_activity(
    contact: Contact,
    client: Client | None = None,
    *,
    events=None,
    campaign_history=None,
    transactional_history=None,
) -> list[ContactActivity]:
    rows: list[ContactActivity] = []
    event_items = list(events)[:8] if events is not None else contact_event_timeline(contact, client)[:8]
    for event in event_items:
        rows.append(
            ContactActivity(
                event.created_at,
                Badge(event.get_event_type_display(), event_tone(event.event_type)),
                event_context(event),
                event.url or "Email event recorded",
            )
        )
    campaign_items = (
        list(campaign_history)[:5] if campaign_history is not None else contact_campaign_history(contact, client)[:5]
    )
    for recipient in campaign_items:
        rows.append(
            ContactActivity(
                recipient.sent_at or recipient.created_at,
                Badge(recipient.get_status_display(), delivery_tone(recipient.status)),
                f"Campaign: {recipient.campaign.subject}",
                f"{recipient.campaign.client.name} / {recipient.campaign.audience.name}",
                href=f"/campaigns/{recipient.campaign_id}/#recipient-{recipient.id}",
                metadata=recipient.last_error or recipient.get_skip_reason_display(),
            )
        )
    tx_items = (
        list(transactional_history)[:5]
        if transactional_history is not None
        else contact_transactional_history(contact, client)[:5]
    )
    for message in tx_items:
        rows.append(
            ContactActivity(
                message.sent_at or message.created_at,
                Badge(message.get_status_display(), delivery_tone(message.status)),
                f"Transactional: {message.template.name}",
                message.subject,
                metadata=message.last_error or metadata_summary(message.metadata),
            )
        )
    rows.sort(key=lambda row: (row.occurred_at is not None, row.occurred_at), reverse=True)
    return rows[:10]


def event_tone(event_type: str) -> str:
    if event_type in {
        EmailEventType.DELIVERED,
        EmailEventType.OPEN,
        EmailEventType.CLICK,
        EmailEventType.SENT,
        EmailEventType.SUBSCRIBE,
    }:
        return "success"
    if event_type in {
        EmailEventType.BOUNCE,
        EmailEventType.COMPLAINT,
        EmailEventType.UNSUBSCRIBE,
        EmailEventType.FAILED,
    }:
        return "danger"
    if event_type in {EmailEventType.SKIPPED, EmailEventType.QUEUED}:
        return "warning"
    return "neutral"


def delivery_tone(status: str) -> str:
    if status in {CampaignRecipientStatus.SENT, TransactionalMessageStatus.SENT}:
        return "success"
    if status in {
        CampaignRecipientStatus.BOUNCED,
        CampaignRecipientStatus.COMPLAINED,
        CampaignRecipientStatus.UNSUBSCRIBED,
        CampaignRecipientStatus.FAILED,
        TransactionalMessageStatus.BOUNCED,
        TransactionalMessageStatus.COMPLAINED,
        TransactionalMessageStatus.FAILED,
    }:
        return "danger"
    if status in {
        CampaignRecipientStatus.SKIPPED,
        TransactionalMessageStatus.SKIPPED,
        CampaignRecipientStatus.PENDING,
        TransactionalMessageStatus.QUEUED,
    }:
        return "warning"
    return "neutral"


def eligibility_items(contact: Contact, subscriptions, *, all_subscriptions=None) -> list[EligibilityItem]:
    subscriptions = list(subscriptions)
    if all_subscriptions is None:
        all_subscriptions = subscriptions
    rows = []
    for subscription in subscriptions:
        rows.append(eligibility_item(contact, subscription, all_subscriptions=all_subscriptions))
    if not rows:
        tx_reasons = transactional_reasons(contact)
        rows.append(
            EligibilityItem(
                scope="No audience/client subscription",
                can_send_marketing=False,
                marketing_reasons=tuple(marketing_contact_reasons(contact)) + ("not subscribed",),
                can_send_transactional=not tx_reasons,
                transactional_reasons=tuple(tx_reasons or ["eligible"]),
            )
        )
    return rows


def eligibility_item(contact: Contact, subscription: Subscription, *, all_subscriptions=None) -> EligibilityItem:
    scope = f"{subscription.audience.name} / {subscription.client.name if subscription.client_id else 'Audience-wide'}"
    marketing = list(marketing_reasons(contact, subscription, all_subscriptions=all_subscriptions))
    tx_reasons = transactional_reasons(contact)
    return EligibilityItem(
        scope=scope,
        can_send_marketing=not marketing,
        marketing_reasons=tuple(marketing or ["eligible"]),
        can_send_transactional=not tx_reasons,
        transactional_reasons=tuple(tx_reasons or ["eligible"]),
    )


def marketing_reasons(contact: Contact, subscription: Subscription, *, all_subscriptions=None):
    # Match campaign sending: audience-wide consent overrides client consent,
    # and verification may be stored globally or on either subscription.
    audience_subscription = next((
        item for item in (all_subscriptions or [subscription])
        if item.audience_id == subscription.audience_id and item.client_id is None
    ), None)
    verified = bool(contact.verified_at or subscription.verified_at
                    or (audience_subscription and audience_subscription.verified_at))
    yield from marketing_contact_reasons(contact, verified=verified)
    if (subscription.client_id and audience_subscription
            and audience_subscription.status == SubscriptionStatus.UNSUBSCRIBED):
        yield "audience unsubscribe"
    if subscription.status == SubscriptionStatus.UNSUBSCRIBED:
        if subscription.client_id:
            yield "client unsubscribe"
        else:
            yield "audience unsubscribe"
    elif subscription.status != SubscriptionStatus.SUBSCRIBED:
        yield f"{subscription.get_status_display().lower()} / not subscribed"


def marketing_contact_reasons(contact: Contact, *, verified: bool | None = None):
    if verified is None:
        verified = contact.verified_at is not None
    if not verified:
        yield "unverified"
    if has_invalid_email_validation(contact):
        yield f"invalid email validation: {email_validation_label(contact.email_validation_status)}"
    if contact.global_unsubscribed_at is not None:
        yield "global unsubscribe"
    if contact.hard_bounced_at is not None:
        yield "hard bounce"
    if contact.complained_at is not None:
        yield "complaint"


def transactional_reasons(contact: Contact):
    reasons = []
    if contact.hard_bounced_at is not None:
        reasons.append("hard bounce")
    if contact.complained_at is not None:
        reasons.append("complaint")
    return tuple(reasons)


def contact_event_timeline(contact: Contact, client: Client | None = None) -> QuerySet[EmailEvent]:
    queryset = EmailEvent.objects.filter(contact=contact).select_related(
        "campaign",
        "campaign__client",
        "campaign__audience",
        "campaign_recipient",
        "transactional_message",
        "transactional_message__template",
        "client",
        "audience",
    )
    if client is not None:
        queryset = queryset.filter(client=client)
    return queryset.order_by("-created_at", "-id")


def event_context(event: EmailEvent) -> str:
    if event.campaign_id:
        return f"Campaign: {event.campaign.subject}"
    if event.transactional_message_id:
        return f"Transactional: {event.transactional_message.template.name}"
    return "Contact event"


def metadata_summary(metadata) -> str:
    if not metadata:
        return ""
    if not isinstance(metadata, dict):
        return str(metadata)

    preferred = []
    reason = metadata.get("reason")
    if reason not in (None, "", [], {}):
        preferred.append(humanize_metadata_value("reason", reason))

    error = metadata.get("error")
    if error not in (None, "", [], {}):
        preferred.append(str(error))

    bounce_type = metadata.get("bounce_type")
    if bounce_type not in (None, "", [], {}):
        preferred.append(humanize_metadata_value("bounce_type", bounce_type))

    complaint_type = metadata.get("complaint_feedback_type") or metadata.get("feedback_type")
    if complaint_type not in (None, "", [], {}):
        preferred.append(humanize_metadata_value("complaint_feedback_type", complaint_type))

    scope = metadata.get("scope")
    if scope not in (None, "", [], {}):
        preferred.append(humanize_metadata_value("scope", scope))

    if preferred:
        return "; ".join(preferred)

    diagnostic_keys = {"ses_message_id", "mail_message_id", "provider_event_id"}
    non_diagnostic_metadata = {key: value for key, value in metadata.items() if key not in diagnostic_keys}
    if not non_diagnostic_metadata:
        return ""
    return json.dumps(non_diagnostic_metadata, sort_keys=True)[:240]


def humanize_metadata_value(key, value) -> str:
    normalized = str(value).replace("_", " ").replace("-", " ").strip()
    compact = normalized.casefold()
    if key == "bounce_type":
        if compact == "permanent":
            return "Permanent bounce"
        if compact == "transient":
            return "Temporary bounce"
        return f"{normalized.capitalize()} bounce"
    if key == "complaint_feedback_type":
        if compact in {"abuse", "fraud"}:
            return "Marked as spam"
        return f"Complaint: {normalized}"
    if key == "reason":
        labels = {
            "snapshot": "Recipient snapshot created",
            "ses rejected": "Rejected by email provider",
            "global unsubscribe": "Globally unsubscribed",
            "client unsubscribe": "Client unsubscribed",
            "audience unsubscribe": "Audience unsubscribed",
            "unverified": "Email not verified",
            "invalid email": "Invalid email address",
        }
        return labels.get(compact, normalized.capitalize())
    if key == "scope":
        return f"{normalized.capitalize()} scope"
    return normalized


def audience_queryset(client: Client | None = None) -> QuerySet[Audience]:
    queryset = Audience.objects.select_related("organization")
    if client is not None:
        queryset = queryset.filter(organization=client.organization)
    subs = Subscription.objects.filter(audience_id=OuterRef("pk")).order_by().values("audience_id")
    campaigns = Campaign.objects.filter(audience_id=OuterRef("pk")).order_by().values("audience_id")
    return queryset.annotate(
        subscription_count=Coalesce(
            Subquery(subs.annotate(c=Count("id")).values("c")[:1], output_field=IntegerField()),
            0,
        ),
        contact_count=Coalesce(
            Subquery(subs.annotate(c=Count("contact_id", distinct=True)).values("c")[:1], output_field=IntegerField()),
            0,
        ),
        campaign_count=Coalesce(
            Subquery(campaigns.annotate(c=Count("id")).values("c")[:1], output_field=IntegerField()),
            0,
        ),
        subscribed_count=Coalesce(
            Subquery(
                subs.filter(status=SubscriptionStatus.SUBSCRIBED).annotate(c=Count("id")).values("c")[:1],
                output_field=IntegerField(),
            ),
            0,
        ),
    ).order_by("organization__slug", "slug")


def audience_detail_queryset() -> QuerySet[Audience]:
    return Audience.objects.select_related("organization")


def audience_list_rows(audiences, client: Client | None = None) -> list[AudienceListRow]:
    audiences = list(audiences)
    campaigns_by_audience = _latest_campaign_by_audience([audience.pk for audience in audiences], client)
    events_by_audience = _latest_event_by_audience([audience.pk for audience in audiences], client)
    return [
        AudienceListRow(
            audience=audience,
            member_count=audience.contact_count,
            subscribed_count=audience.subscribed_count,
            inactive_count=audience_inactive_count(audience),
            suppressed_count=audience_suppressed_count(audience),
            campaign_count=audience.campaign_count,
            recent_campaign=campaigns_by_audience.get(audience.pk),
            recent_event=events_by_audience.get(audience.pk),
        )
        for audience in audiences
    ]


def _latest_campaign_by_audience(audience_ids, client: Client | None = None) -> dict:
    """Latest campaign per audience in 2 bounded queries (was 1 query/row)."""
    ids = [pk for pk in dict.fromkeys(audience_ids) if pk is not None]
    if not ids:
        return {}
    scope = Campaign.objects.filter(audience_id=OuterRef("pk"))
    if client is not None:
        scope = scope.filter(client=client)
    latest_ids = [
        pk
        for pk in Audience.objects.filter(pk__in=ids)
        .order_by()
        .annotate(latest_id=Subquery(scope.order_by("-updated_at", "-id").values("id")[:1]))
        .values_list("latest_id", flat=True)
        if pk is not None
    ]
    if not latest_ids:
        return {}
    return {
        campaign.audience_id: campaign
        for campaign in Campaign.objects.filter(pk__in=latest_ids).select_related("client", "audience")
    }


def _latest_event_by_audience(audience_ids, client: Client | None = None) -> dict:
    """Latest event per audience in 2 bounded queries (was 1 query/row)."""
    ids = [pk for pk in dict.fromkeys(audience_ids) if pk is not None]
    if not ids:
        return {}
    scope = EmailEvent.objects.filter(audience_id=OuterRef("pk"))
    if client is not None:
        scope = scope.filter(client=client)
    latest_ids = [
        pk
        for pk in Audience.objects.filter(pk__in=ids)
        .order_by()
        .annotate(latest_id=Subquery(scope.order_by("-created_at", "-id").values("id")[:1]))
        .values_list("latest_id", flat=True)
        if pk is not None
    ]
    if not latest_ids:
        return {}
    return {
        event.audience_id: event
        for event in EmailEvent.objects.filter(pk__in=latest_ids).select_related(
            "contact", "campaign", "client", "audience"
        )
    }


def audience_contacts(audience: Audience) -> QuerySet[Contact]:
    return Contact.objects.filter(
        id__in=Subscription.objects.filter(audience=audience).order_by().values("contact_id")
    )


def audience_inactive_count(audience: Audience) -> int:
    rec_scope = CampaignRecipient.objects.filter(campaign__audience=audience).order_by()
    evt_scope = EmailEvent.objects.filter(audience=audience).order_by()
    sent_q = Q(id__in=rec_scope.filter(sent_at__isnull=False).values("contact_id")) | Q(
        id__in=rec_scope.filter(status=CampaignRecipientStatus.SENT).values("contact_id")
    )
    engaged_q = (
        Q(id__in=rec_scope.filter(first_opened_at__isnull=False).values("contact_id"))
        | Q(id__in=rec_scope.filter(first_clicked_at__isnull=False).values("contact_id"))
        | Q(id__in=evt_scope.filter(event_type__in=[EmailEventType.OPEN, EmailEventType.CLICK]).values("contact_id"))
    )
    return audience_contacts(audience).filter(sent_q).exclude(engaged_q).count()


def audience_suppressed_count(audience: Audience) -> int:
    unsubscribed_ids = (
        Subscription.objects.filter(audience=audience, status=SubscriptionStatus.UNSUBSCRIBED)
        .order_by()
        .values("contact_id")
    )
    return (
        audience_contacts(audience)
        .filter(
            Q(global_unsubscribed_at__isnull=False)
            | Q(hard_bounced_at__isnull=False)
            | Q(complained_at__isnull=False)
            | Q(id__in=unsubscribed_ids)
        )
        .count()
    )


def audience_summary(audience: Audience, client: Client | None = None) -> list[Stat]:
    contacts = audience_contacts(audience)
    if client is not None:
        contacts = Contact.objects.filter(
            id__in=Subscription.objects.filter(audience=audience, client=client).order_by().values("contact_id")
        )
    rec_scope = CampaignRecipient.objects.filter(campaign__audience=audience).order_by()
    evt_scope = EmailEvent.objects.filter(audience=audience).order_by()
    client_id = client.id if client is not None else None
    anchor = "#audience-members"

    def linked_count(**signal) -> int:
        # Count the exact queryset that audience_detail paginates for this signal so the
        # displayed number always equals the linked (filtered) members list total.
        filters = ContactExplorerFilters(audience_id=audience.id, client_id=client_id, **signal)
        return contact_explorer_queryset(filters).count()

    return [
        Stat("members", "Members", linked_count(), href=f"?{anchor}"),
        Stat(
            "subscribed",
            "Subscribed",
            linked_count(subscription_status=SubscriptionStatus.SUBSCRIBED.value),
            href=f"?subscription_status={SubscriptionStatus.SUBSCRIBED.value}{anchor}",
        ),
        Stat(
            "pending",
            "Pending",
            linked_count(subscription_status=SubscriptionStatus.PENDING.value),
            href=f"?subscription_status={SubscriptionStatus.PENDING.value}{anchor}",
        ),
        Stat(
            "unsubscribed",
            "Unsubscribed",
            linked_count(subscription_status=SubscriptionStatus.UNSUBSCRIBED.value),
            href=f"?subscription_status={SubscriptionStatus.UNSUBSCRIBED.value}{anchor}",
        ),
        Stat(
            "verified",
            "Verified",
            linked_count(verified_state="verified"),
            href=f"?verified=verified{anchor}",
        ),
        Stat(
            "unverified",
            "Unverified",
            linked_count(verified_state="unverified"),
            href=f"?verified=unverified{anchor}",
        ),
        Stat("inactive", "Inactive", audience_inactive_count(audience)),
        Stat(
            "global_unsubscribed",
            "Global unsubscribed",
            linked_count(suppression_state="global_unsubscribed"),
            href=f"?suppression=global_unsubscribed{anchor}",
        ),
        Stat(
            "hard_bounced",
            "Hard bounced",
            linked_count(suppression_state="hard_bounced"),
            href=f"?suppression=hard_bounced{anchor}",
        ),
        Stat(
            "complained",
            "Complained",
            linked_count(suppression_state="complained"),
            href=f"?suppression=complained{anchor}",
        ),
        Stat(
            "opened",
            "Opened",
            contacts.filter(
                Q(id__in=rec_scope.filter(first_opened_at__isnull=False).values("contact_id"))
                | Q(id__in=evt_scope.filter(event_type=EmailEventType.OPEN).values("contact_id"))
            ).count(),
        ),
        Stat(
            "clicked",
            "Clicked",
            contacts.filter(
                Q(id__in=rec_scope.filter(first_clicked_at__isnull=False).values("contact_id"))
                | Q(id__in=evt_scope.filter(event_type=EmailEventType.CLICK).values("contact_id"))
            ).count(),
        ),
    ]


def count_by_field(queryset, field, *, choices=None):
    raw_counts = dict(queryset.order_by().values_list(field).annotate(count=Count("id")))
    if choices:
        return [(label, count) for value, label in choices if (count := raw_counts.get(value, 0)) > 0]
    return sorted(raw_counts.items())


def email_validation_label(value) -> str:
    labels = {
        EmailValidationStatus.UNKNOWN: "No validation data",
        EmailValidationStatus.VALID: "Valid email",
        EmailValidationStatus.INVALID_SYNTAX: "Malformed email",
        EmailValidationStatus.NO_MX: "Missing email DNS",
        EmailValidationStatus.DISPOSABLE: "Disposable email",
        EmailValidationStatus.RISKY: "Risky email",
        EmailValidationStatus.MANUALLY_INVALID: "Marked invalid",
        EmailValidationStatus.EXTERNALLY_VALIDATED: "Validated externally",
    }
    return labels.get(value, str(value))


def audience_breakdowns(audience: Audience, client: Client | None = None):
    contacts = audience_contacts(audience)
    campaign_recipients = CampaignRecipient.objects.filter(campaign__audience=audience).order_by()
    if client is not None:
        contacts = Contact.objects.filter(
            id__in=Subscription.objects.filter(audience=audience, client=client).order_by().values("contact_id")
        )
        campaign_recipients = campaign_recipients.filter(campaign__client=client)
    return {
        "validation": count_by_field(
            contacts,
            "email_validation_status",
            choices=[(value, email_validation_label(value)) for value, _label in EmailValidationStatus.choices],
        ),
        "tags": Tag.objects.filter(audience=audience)
        .annotate(count=Count("contact_tags", distinct=True))
        .order_by("slug"),
        "campaign_statuses": count_by_field(
            campaign_recipients,
            "status",
            choices=CampaignRecipientStatus.choices,
        ),
        "skip_reasons": count_by_field(
            campaign_recipients.exclude(skip_reason=""),
            "skip_reason",
            choices=CampaignRecipientSkipReason.choices,
        ),
    }


def audience_campaign_history(audience: Audience, client: Client | None = None) -> QuerySet[Campaign]:
    queryset = Campaign.objects.filter(audience=audience).select_related("client")
    if client is not None:
        queryset = queryset.filter(client=client)
    return queryset.order_by("-created_at", "-id")


def audience_recent_events(
    audience: Audience, event_type: str = "", client: Client | None = None
) -> QuerySet[EmailEvent]:
    queryset = EmailEvent.objects.filter(audience=audience).select_related(
        "contact",
        "campaign",
        "campaign__client",
        "transactional_message",
        "client",
    )
    if client is not None:
        queryset = queryset.filter(client=client)
    if valid_choice(event_type, EmailEventType):
        queryset = queryset.filter(event_type=event_type)
    return queryset.order_by("-created_at", "-id")
