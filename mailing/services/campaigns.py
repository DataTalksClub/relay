import logging
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.db.models import Case, CharField, Count, Exists, F, OuterRef, Q, Sum, Value, When
from django.utils import timezone

import taskdeck
from mailing.enqueue import batch_campaign_enqueues, enqueue_campaign_email
from mailing.models import (
    Campaign,
    CampaignDispatch,
    CampaignRecipient,
    CampaignRecipientSkipReason,
    CampaignRecipientStatus,
    CampaignStatus,
    CategoryPreference,
    Contact,
    EmailEvent,
    EmailEventType,
    RecipientListMember,
    Subscription,
    SubscriptionStatus,
    normalize_tag_filter,
)
from mailing.queue_contracts import CAMPAIGN_EMAIL_CONTRACT, CONTRACT_VERSION, validate_campaign_email_message
from mailing.services.campaign_composer import composer_send_issues
from mailing.services.contacts import (
    NON_DELIVERABLE_EMAIL_VALIDATION_STATUSES,
    has_invalid_email_validation,
    is_verified_for_marketing,
)
from mailing.services.insert_select import insert_select


@dataclass(frozen=True)
class SnapshotResult:
    campaign_id: int
    created_count: int
    recipient_count: int
    skipped_count: int


@dataclass(frozen=True)
class RecipientPreviewRow:
    email: str
    status: str
    skip_reason: str

    @property
    def status_label(self):
        return "Eligible" if self.status == CampaignRecipientStatus.PENDING else "Excluded"

    @property
    def skip_reason_label(self):
        return CampaignRecipientSkipReason(self.skip_reason).label if self.skip_reason else ""


@dataclass(frozen=True)
class RecipientEstimate:
    total_candidates: int
    tag_filtered_count: int
    recipient_count: int
    skipped_count: int
    skip_reason_counts: dict[str, int]
    preview_rows: list[RecipientPreviewRow]

    @property
    def skip_reason_labels(self):
        return {CampaignRecipientSkipReason(reason).label: count for reason, count in self.skip_reason_counts.items()}


@dataclass(frozen=True)
class QueueCampaignResult:
    campaign_id: int
    queued: bool
    batch_count: int
    recipient_count: int
    skipped_count: int
    scheduled: bool = False
    snapshotting: bool = False


@dataclass(frozen=True)
class QueueRequestResult:
    campaign_id: int
    accepted: bool
    already_queued: bool
    scheduled: bool
    snapshotting: bool
    status: str

    batch_count: int = 0

class CampaignRecipientConflict(Exception):
    """The requested recipient transition is not allowed from its current state."""


@dataclass(frozen=True)
class RecountResult:
    campaign_id: int
    total_candidates: int
    tag_filtered_count: int
    recipient_count: int
    skipped_count: int
    skip_reason_counts: dict[str, int]


@dataclass(frozen=True)
class RetryRecipientResult:
    campaign_id: int
    recipient_id: int
    retried: bool


def recount_campaign_audience(campaign):
    """Count the filtered audience without exposing any identity.

    The parity target is AISL's operator recount: the same queryset the send
    path will snapshot, reduced to numbers only. ``preview_limit=0`` keeps
    preview rows (recipient addresses) out of the result entirely.
    """
    estimate = estimate_campaign_recipients(campaign, preview_limit=0)
    return RecountResult(
        campaign_id=campaign.id,
        total_candidates=estimate.total_candidates,
        tag_filtered_count=estimate.tag_filtered_count,
        recipient_count=estimate.recipient_count,
        skipped_count=estimate.skipped_count,
        skip_reason_counts=estimate.skip_reason_counts,
    )


@transaction.atomic
def retry_campaign_recipient(campaign, recipient_id):
    """Requeue one failed delivery for sending.

    Only ``failed`` recipients are retryable: ``sent`` really went out,
    ``skipped`` was never meant to go out, and provider-level ``bounced`` and
    ``complained`` outcomes must not be mailed again. The campaign must still
    be able to send (anything but draft or cancelled). Like the original
    queueing, the batch is dispatched only after the transaction commits.
    """
    campaign = (
        Campaign.objects.select_for_update()
        .select_related("audience", "client")
        .get(pk=campaign.pk)
    )
    recipient = (
        CampaignRecipient.objects.select_for_update()
        .select_related("contact")
        .get(pk=recipient_id, campaign=campaign)
    )

    if campaign.status in {CampaignStatus.DRAFT, CampaignStatus.SCHEDULED, CampaignStatus.CANCELLED}:
        raise CampaignRecipientConflict("campaign_cannot_send")

    if recipient.status != CampaignRecipientStatus.FAILED:
        raise CampaignRecipientConflict("recipient_not_failed")

    prior_error = recipient.last_error
    recipient.status = CampaignRecipientStatus.PENDING
    recipient.last_error = f"{prior_error} [retried from failed]".strip()[:2000]
    recipient.save(update_fields=["status", "last_error", "updated_at"])

    if campaign.status in {CampaignStatus.SENT, CampaignStatus.FAILED}:
        campaign.status = CampaignStatus.SENDING
        campaign.save(update_fields=["status", "updated_at"])

    now = timezone.now()
    EmailEvent.objects.create(
        campaign=campaign,
        campaign_recipient=recipient,
        contact=recipient.contact,
        client=campaign.client,
        audience=campaign.audience,
        event_type=EmailEventType.QUEUED,
        metadata={"retry": True},
        created_at=now,
    )

    payload = {
        "contract": CAMPAIGN_EMAIL_CONTRACT,
        "version": CONTRACT_VERSION,
        "campaign_id": campaign.id,
        "batch_id": f"campaign-{campaign.id}-retry-{recipient.id}",
        "campaign_recipient_ids": [recipient.id],
        "idempotency_key": f"campaign-{campaign.id}-retry-{recipient.id}",
    }
    validate_campaign_email_message(payload)
    send_correlation_id = uuid.uuid4()
    taskdeck.enqueue_on_commit(_enqueue_campaign_batch, send_correlation_id, payload)

    return RetryRecipientResult(campaign_id=campaign.id, recipient_id=recipient.id, retried=True)


@transaction.atomic
def snapshot_campaign_recipients(campaign):
    campaign = (
        Campaign.objects.select_for_update()
        .select_related("audience", "client")
        .get(pk=campaign.pk)
    )
    include_tags = normalize_tag_filter(campaign.include_tags)
    exclude_tags = normalize_tag_filter(campaign.exclude_tags)
    subscriptions = Subscription.objects.filter(contact_id=OuterRef("pk"), audience_id=campaign.audience_id)
    audience_subs = subscriptions.filter(client__isnull=True)
    client_subs = subscriptions.filter(client_id=campaign.client_id)
    reason = CampaignRecipientSkipReason
    rules = [
        When(hard_bounced_at__isnull=False, then=Value(reason.HARD_BOUNCE)),
        When(complained_at__isnull=False, then=Value(reason.COMPLAINT)),
        When(global_unsubscribed_at__isnull=False, then=Value(reason.GLOBAL_UNSUBSCRIBE)),
        When(email_validation_status__in=NON_DELIVERABLE_EMAIL_VALIDATION_STATUSES, then=Value(reason.INVALID_EMAIL)),
        When(Q(verified_at__isnull=True)
             & ~Exists(audience_subs.filter(verified_at__isnull=False))
             & ~Exists(client_subs.filter(verified_at__isnull=False)), then=Value(reason.UNVERIFIED)),
        When(Exists(audience_subs.filter(status=SubscriptionStatus.UNSUBSCRIBED)), then=Value(reason.AUDIENCE_UNSUBSCRIBE)),
        When(Exists(client_subs.filter(status=SubscriptionStatus.UNSUBSCRIBED)), then=Value(reason.CLIENT_UNSUBSCRIBE)),
        When(~Exists(client_subs.filter(status=SubscriptionStatus.SUBSCRIBED)), then=Value(reason.SUPPRESSED)),
    ]
    if campaign.category_tag:
        rules.append(When(Exists(CategoryPreference.objects.filter(
            contact_id=OuterRef("pk"), audience_id=campaign.audience_id, client_id=campaign.client_id,
            tag=campaign.category_tag, enabled=False,
        )), then=Value(reason.CLIENT_UNSUBSCRIBE)))
    candidates = _candidate_contacts(campaign, include_tags, exclude_tags).filter(
        ~Exists(CampaignRecipient.objects.filter(campaign_id=campaign.pk, contact_id=OuterRef("pk")))
    ).annotate(snapshot_skip=Case(*rules, default=Value(""), output_field=CharField()))
    created_count = insert_select(CampaignRecipient, candidates, {
        "campaign_id": campaign.pk, "contact_id": F("pk"), "email": F("email"),
        "skip_reason": F("snapshot_skip"),
        "status": Case(When(snapshot_skip="", then=Value(CampaignRecipientStatus.PENDING)),
                       default=Value(CampaignRecipientStatus.SKIPPED)),
    }, conflict_fields=("campaign", "contact"))

    counts = _refresh_campaign_counts(campaign)
    return SnapshotResult(
        campaign_id=campaign.id,
        created_count=created_count,
        recipient_count=counts["recipient_count"],
        skipped_count=counts["skipped_count"],
    )


def estimate_campaign_recipients(campaign, *, preview_limit=25):
    include_tags = normalize_tag_filter(campaign.include_tags)
    exclude_tags = normalize_tag_filter(campaign.exclude_tags)
    total_candidates = _base_candidate_contacts(campaign).count()
    candidate_contacts = list(_candidate_contacts(campaign, include_tags, exclude_tags))
    tag_filtered_count = total_candidates - len(candidate_contacts)
    bulk = _bulk_skip_context(campaign, [c.pk for c in candidate_contacts])
    recipient_count = 0
    skipped_count = 0
    skip_reason_counts = {reason: 0 for reason, _label in CampaignRecipientSkipReason.choices}
    preview_rows = []

    for contact in candidate_contacts:
        skip_reason = _skip_reason_bulk(contact, campaign, bulk)
        if skip_reason:
            skipped_count += 1
            skip_reason_counts[skip_reason] += 1
            status = CampaignRecipientStatus.SKIPPED
        else:
            recipient_count += 1
            status = CampaignRecipientStatus.PENDING

        if len(preview_rows) < preview_limit:
            preview_rows.append(
                RecipientPreviewRow(
                    email=contact.email,
                    status=status,
                    skip_reason=skip_reason,
                )
            )

    return RecipientEstimate(
        total_candidates=total_candidates,
        tag_filtered_count=tag_filtered_count,
        recipient_count=recipient_count,
        skipped_count=skipped_count,
        skip_reason_counts={reason: count for reason, count in skip_reason_counts.items() if count},
        preview_rows=preview_rows,
    )


def _bulk_skip_context(campaign, contact_ids):
    """Fetch per-contact eligibility inputs in bulk (3 queries, not 3N).

    Returns (audience_subs, client_subs, disabled_category_ids) keyed by
    contact id. Keeps estimate/detail pages under 2 s for large audiences.
    """
    contact_ids = list(dict.fromkeys(contact_ids))
    if not contact_ids:
        return ({}, {}, set())
    audience_subs = {
        row["contact_id"]: row
        for row in Subscription.objects.filter(
            contact_id__in=contact_ids, audience=campaign.audience, client__isnull=True
        ).values("contact_id", "status", "verified_at")
    }
    client_subs = {
        row["contact_id"]: row
        for row in Subscription.objects.filter(
            contact_id__in=contact_ids, audience=campaign.audience, client=campaign.client
        ).values("contact_id", "status", "verified_at")
    }
    disabled = set()
    if campaign.category_tag:
        disabled = set(
            CategoryPreference.objects.filter(
                contact_id__in=contact_ids,
                audience=campaign.audience,
                client=campaign.client,
                tag=campaign.category_tag,
                enabled=False,
            ).values_list("contact_id", flat=True)
        )
    return (audience_subs, client_subs, disabled)


def _skip_reason_bulk(contact, campaign, bulk):
    if contact.hard_bounced_at is not None:
        return CampaignRecipientSkipReason.HARD_BOUNCE
    if contact.complained_at is not None:
        return CampaignRecipientSkipReason.COMPLAINT
    if contact.global_unsubscribed_at is not None:
        return CampaignRecipientSkipReason.GLOBAL_UNSUBSCRIBE
    if has_invalid_email_validation(contact):
        return CampaignRecipientSkipReason.INVALID_EMAIL
    audience_subs, client_subs, disabled = bulk
    audience_subscription = audience_subs.get(contact.pk)
    client_subscription = client_subs.get(contact.pk)

    if not _is_verified_bulk(contact, audience_subscription, client_subscription):
        return CampaignRecipientSkipReason.UNVERIFIED
    if audience_subscription and audience_subscription["status"] == SubscriptionStatus.UNSUBSCRIBED:
        return CampaignRecipientSkipReason.AUDIENCE_UNSUBSCRIBE
    if client_subscription and client_subscription["status"] == SubscriptionStatus.UNSUBSCRIBED:
        return CampaignRecipientSkipReason.CLIENT_UNSUBSCRIBE
    if not client_subscription or client_subscription["status"] != SubscriptionStatus.SUBSCRIBED:
        return CampaignRecipientSkipReason.SUPPRESSED
    if campaign.category_tag and contact.pk in disabled:
        return CampaignRecipientSkipReason.CLIENT_UNSUBSCRIBE
    return ""


def _is_verified_bulk(contact, audience_subscription, client_subscription):
    """Mirror contacts.is_verified_for_marketing without per-row queries."""
    if contact.verified_at is not None:
        return True
    for sub in (audience_subscription, client_subscription):
        if sub and sub.get("verified_at") is not None:
            return True
    return False


def _enqueue_campaign_batches(correlation_id, payloads):
    with batch_campaign_enqueues(correlation_id):
        for payload in payloads:
            _enqueue_campaign_batch(correlation_id, payload)


def _enqueue_campaign_batch(correlation_id, payload):
    """Enqueue one batch under the send's shared correlation id."""
    with taskdeck.bind_correlation(correlation_id):
        enqueue_campaign_email(payload)


class CampaignNotReady(ValueError):
    """A draft cannot safely be queued for immediate sending."""


def campaign_send_issues(campaign):
    issues = list(composer_send_issues(campaign))
    if not campaign.subject.strip():
        issues.append("Add a subject before sending.")
    if not campaign.html_body.strip() and not campaign.text_body.strip():
        issues.append("Add HTML or plain-text email content before sending.")
    if campaign.scheduled_at and timezone.is_naive(campaign.scheduled_at):
        issues.append("Choose a send time with an explicit timezone.")
    return issues


def queue_campaign(campaign, *, batch_size=None, dispatch_due=False, now=None,
                   expected_revision=None, reject_past_schedule=False):
    batch_size = batch_size or getattr(settings, "CAMPAIGN_EMAIL_BATCH_SIZE", 100)
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    with transaction.atomic():
        locked_campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
        now = now or timezone.now()
        dispatching = dispatch_due and locked_campaign.status == CampaignStatus.SCHEDULED
        if locked_campaign.status != CampaignStatus.DRAFT and not dispatching:
            return QueueCampaignResult(
                campaign_id=locked_campaign.id,
                queued=False,
                batch_count=0,
                recipient_count=locked_campaign.recipient_count,
                skipped_count=locked_campaign.skipped_count,
                scheduled=locked_campaign.status == CampaignStatus.SCHEDULED,
            )

        if expected_revision is not None and str(expected_revision) != locked_campaign.updated_at.isoformat():
            raise CampaignNotReady("This campaign changed after review. Review the updated message and send time before confirming.")
        if (reject_past_schedule and locked_campaign.status == CampaignStatus.DRAFT
                and locked_campaign.scheduled_at is not None and locked_campaign.scheduled_at <= now):
            raise CampaignNotReady("The scheduled time has passed. Choose a future time or explicitly choose Send now before confirming.")

        issues = campaign_send_issues(locked_campaign)
        if issues:
            raise CampaignNotReady(" ".join(issues))

        if locked_campaign.scheduled_at and locked_campaign.scheduled_at > now:
            if locked_campaign.status == CampaignStatus.DRAFT:
                locked_campaign.status = CampaignStatus.SCHEDULED
                locked_campaign.save(update_fields=["status", "updated_at"])
                return QueueCampaignResult(locked_campaign.id, True, 0, 0, 0, scheduled=True)
            return QueueCampaignResult(locked_campaign.id, False, 0, locked_campaign.recipient_count,
                                       locked_campaign.skipped_count, scheduled=True)
        if dispatching and locked_campaign.scheduled_at is None:
            raise CampaignNotReady("The scheduled campaign has no send time.")

        snapshot_campaign_recipients(locked_campaign)
        locked_campaign.refresh_from_db()
        recipient_ids = list(
            CampaignRecipient.objects.filter(
                campaign=locked_campaign,
                status=CampaignRecipientStatus.PENDING,
            )
            .order_by("id")
            .values_list("id", flat=True)
        )
        payloads = _campaign_email_payloads(locked_campaign.id, recipient_ids, batch_size)
        locked_campaign.status = CampaignStatus.SENT if dispatching and not payloads else CampaignStatus.QUEUED
        if dispatching and not payloads:
            locked_campaign.sent_at = now
        locked_campaign.save(update_fields=["status", "sent_at", "updated_at"])
        _append_queue_audit_events(locked_campaign, recipient_ids, len(payloads))

        # Enqueue only once this transaction commits.
        #
        # Previously this loop ran after the `atomic` block with no on-commit
        # hook. When `queue_campaign` is called inside an outer transaction the
        # block above is only a savepoint, so the batches were dispatched
        # before the outer commit -- a subsequent rollback left a campaign that
        # was never marked QUEUED with its sends already in flight, and a
        # failure part-way through the loop left a QUEUED campaign with only
        # some batches dispatched. Both are unrecoverable once mail leaves.
        #
        # The correlation id must be bound *inside* the deferred callable, not
        # around this loop: on-commit callbacks run after the block exits, by
        # which point a context manager here would already have reset.
        send_correlation_id = uuid.uuid4()
        if dispatching:
            dispatch, _ = CampaignDispatch.objects.get_or_create(
                campaign=locked_campaign,
                defaults={"payloads": payloads, "correlation_id": send_correlation_id},
            )
            transaction.on_commit(lambda: _enqueue_scheduled_dispatch(dispatch.pk), robust=True)
        else:
            transaction.on_commit(lambda: _enqueue_campaign_batches(send_correlation_id, payloads))


    return QueueCampaignResult(
        campaign_id=locked_campaign.id,
        queued=True,
        batch_count=len(payloads),
        recipient_count=locked_campaign.recipient_count,
        skipped_count=locked_campaign.skipped_count,
    )


logger = logging.getLogger(__name__)


def request_campaign_queue(campaign, *, expected_revision=None, reject_past_schedule=False,
                           batch_size=None):
    """Complete the snapshot and queue synchronously before returning."""
    result = queue_campaign(
        campaign, expected_revision=expected_revision,
        reject_past_schedule=reject_past_schedule, batch_size=batch_size,
    )
    campaign.refresh_from_db()
    return QueueRequestResult(
        campaign.pk, result.queued, not result.queued, result.scheduled, False, campaign.status, result.batch_count,
    )


def _enqueue_scheduled_dispatch(dispatch_id):
    """Task inserts and the completion marker commit together on the DB backend.

    An external backend may redeliver after an uncertain enqueue; recipient
    row locks and persisted provider IDs keep repeated task execution safe.
    """
    try:
        with transaction.atomic():
            dispatch = CampaignDispatch.objects.select_for_update().select_related("campaign").get(pk=dispatch_id)
            if dispatch.enqueued_at is not None:
                return False
            if dispatch.campaign.status != CampaignStatus.CANCELLED:
                for payload in dispatch.payloads:
                    _enqueue_campaign_batch(dispatch.correlation_id, payload)
            dispatch.enqueued_at = timezone.now()
            dispatch.last_error = ""
            dispatch.save(update_fields=["enqueued_at", "last_error", "updated_at"])
            return True
    except Exception as exc:
        CampaignDispatch.objects.filter(pk=dispatch_id).update(last_error=str(exc)[:2000])
        raise


def recover_campaign_dispatches(*, limit=100):
    recovered = 0
    for dispatch_id in CampaignDispatch.objects.filter(enqueued_at__isnull=True).order_by("id").values_list("id", flat=True)[:limit]:
        try:
            recovered += bool(_enqueue_scheduled_dispatch(dispatch_id))
        except Exception:
            logger.exception("Scheduled campaign dispatch %s could not be enqueued", dispatch_id)
    return recovered


def dispatch_due_campaigns(*, now=None, limit=100):
    """Called by Relay's existing scheduler; delayed work is never fired early."""
    now = now or timezone.now()
    recover_campaign_dispatches(limit=limit)
    due_ids = list(Campaign.objects.filter(status=CampaignStatus.SCHEDULED, scheduled_at__lte=now)
                   .order_by("scheduled_at", "id").values_list("id", flat=True)[:limit])
    results = []
    for campaign_id in due_ids:
        try:
            result = queue_campaign(Campaign(pk=campaign_id), dispatch_due=True, now=now)
            if result.queued:
                results.append(result)
        except CampaignNotReady as exc:
            with transaction.atomic():
                failed = Campaign.objects.select_for_update().get(pk=campaign_id)
                if failed.status == CampaignStatus.SCHEDULED:
                    failed.status = CampaignStatus.FAILED
                    failed.metadata = dict(failed.metadata or {}) | {"scheduling_error": str(exc)}
                    failed.save(update_fields=["status", "metadata", "updated_at"])
            logger.exception("Scheduled campaign %s is not ready for dispatch", campaign_id)
    return results


def _candidate_contacts(campaign, include_tags, exclude_tags):
    queryset = _base_candidate_contacts(campaign)

    if include_tags:
        queryset = queryset.filter(contact_tags__tag__audience=campaign.audience, contact_tags__tag__slug__in=include_tags)
        queryset = queryset.annotate(
            matched_include_tag_count=Count(
                "contact_tags__tag__slug",
                filter=Q(contact_tags__tag__audience=campaign.audience, contact_tags__tag__slug__in=include_tags),
                distinct=True,
            )
        ).filter(matched_include_tag_count=len(include_tags))

    if exclude_tags:
        queryset = queryset.exclude(
            contact_tags__tag__audience=campaign.audience,
            contact_tags__tag__slug__in=exclude_tags,
        )

    return queryset


def _base_candidate_contacts(campaign):
    if campaign.recipient_list_key:
        contacts = (
            RecipientListMember.objects.filter(
                recipient_list__audience=campaign.audience,
                recipient_list__client=campaign.client,
                recipient_list__key=campaign.recipient_list_key,
                active=True,
            )
            .order_by("contact__normalized_email", "contact_id")
            .distinct()
            .values_list("contact", flat=True)
        )
        return Contact.objects.filter(id__in=contacts).order_by("normalized_email", "id")

    contacts = (
        Subscription.objects.filter(audience=campaign.audience)
        .filter(Q(client=campaign.client) | Q(client__isnull=True))
        .select_related("contact")
        .order_by("contact__normalized_email", "contact_id")
        .distinct()
        .values_list("contact", flat=True)
    )
    return Contact.objects.filter(id__in=contacts).order_by("normalized_email", "id")


def _campaign_email_payloads(campaign_id, recipient_ids, batch_size):
    payloads = []
    for batch_index, offset in enumerate(range(0, len(recipient_ids), batch_size), start=1):
        batch_id = f"campaign-{campaign_id}-batch-{batch_index:04d}"
        payload = {
            "contract": CAMPAIGN_EMAIL_CONTRACT,
            "version": CONTRACT_VERSION,
            "campaign_id": campaign_id,
            "batch_id": batch_id,
            "campaign_recipient_ids": recipient_ids[offset : offset + batch_size],
            "idempotency_key": batch_id,
        }
        validate_campaign_email_message(payload)
        payloads.append(payload)
    return payloads


def _append_queue_audit_events(campaign, recipient_ids, batch_count):
    if not recipient_ids:
        return
    insert_select(EmailEvent, CampaignRecipient.objects.filter(id__in=recipient_ids), {
        "campaign_id": campaign.pk, "campaign_recipient_id": F("pk"), "contact_id": F("contact_id"),
        "client_id": campaign.client_id, "audience_id": campaign.audience_id,
        "event_type": EmailEventType.QUEUED, "metadata": {"batch_count": batch_count},
    })


def _recipient_status(contact, campaign):
    if _skip_reason(contact, campaign):
        return CampaignRecipientStatus.SKIPPED
    return CampaignRecipientStatus.PENDING


def _skip_reason(contact, campaign):
    if contact.hard_bounced_at is not None:
        return CampaignRecipientSkipReason.HARD_BOUNCE
    if contact.complained_at is not None:
        return CampaignRecipientSkipReason.COMPLAINT
    if contact.global_unsubscribed_at is not None:
        return CampaignRecipientSkipReason.GLOBAL_UNSUBSCRIBE
    if has_invalid_email_validation(contact):
        return CampaignRecipientSkipReason.INVALID_EMAIL
    audience_subscription = _subscription_for(contact, campaign, client=None)
    client_subscription = _subscription_for(contact, campaign, client=campaign.client)

    if not is_verified_for_marketing(
        contact,
        audience_subscription,
        client_subscription,
    ):
        return CampaignRecipientSkipReason.UNVERIFIED

    if audience_subscription and audience_subscription.status == SubscriptionStatus.UNSUBSCRIBED:
        return CampaignRecipientSkipReason.AUDIENCE_UNSUBSCRIBE

    if client_subscription and client_subscription.status == SubscriptionStatus.UNSUBSCRIBED:
        return CampaignRecipientSkipReason.CLIENT_UNSUBSCRIBE
    if not client_subscription or client_subscription.status != SubscriptionStatus.SUBSCRIBED:
        return CampaignRecipientSkipReason.SUPPRESSED
    if _has_disabled_category_preference(contact, campaign):
        return CampaignRecipientSkipReason.CLIENT_UNSUBSCRIBE

    return ""


def _subscription_for(contact, campaign, client):
    return Subscription.objects.filter(
        contact=contact,
        audience=campaign.audience,
        client=client,
    ).first()


def _has_disabled_category_preference(contact, campaign):
    if not campaign.category_tag:
        return False
    return CategoryPreference.objects.filter(
        contact=contact,
        audience=campaign.audience,
        client=campaign.client,
        tag=campaign.category_tag,
        enabled=False,
    ).exists()


def _refresh_campaign_counts(campaign):
    aggregate = CampaignRecipient.objects.filter(campaign=campaign).aggregate(
        recipient_count=Count("pk", filter=~Q(status=CampaignRecipientStatus.SKIPPED)),
        skipped_count=Count("pk", filter=Q(status=CampaignRecipientStatus.SKIPPED)),
        sent_count=Count("pk", filter=Q(status=CampaignRecipientStatus.SENT)),
        delivered_count=Count("pk", filter=Q(delivered_at__isnull=False)),
        unique_open_count=Count("pk", filter=Q(first_opened_at__isnull=False)),
        unique_click_count=Count("pk", filter=Q(first_clicked_at__isnull=False)),
        open_count=Sum("open_count"), click_count=Sum("click_count"),
    )
    for field, value in aggregate.items():
        setattr(campaign, field, value or 0)
    campaign.save(
        update_fields=[
            "recipient_count",
            "skipped_count",
            "sent_count",
            "delivered_count",
            "unique_open_count",
            "unique_click_count",
            "open_count",
            "click_count",
            "include_tags",
            "exclude_tags",
            "updated_at",
        ]
    )
    return {"recipient_count": campaign.recipient_count, "skipped_count": campaign.skipped_count}
