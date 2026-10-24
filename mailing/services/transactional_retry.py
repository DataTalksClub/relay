"""Operator retry for failed transactional messages.

Replaces the manual shell workaround used for message 8017 on 2026-10-24
(``status failed->queued`` plus ``enqueue_transactional_email(
build_transactional_queue_payload(...))`` in a prod shell).

A retry keeps the same message row and idempotency key, re-resolves the
sender (so a fixed sending-domain/route applies at retry time), re-queues
through the normal ``enqueue_transactional_email`` path, records a QUEUED
email event (attempt history) plus a ``retry_count`` in metadata, and writes
one ``operator_audits`` row per retried message.

Guardrails:

- Only ``failed`` messages retry without ``--force``. Any other status
  needs explicit force plus a reason, so an already sent/delivered message
  is never duplicate-sent by accident.
- Suppressed (hard bounce / complaint) and blocklisted recipients never
  retry, even with force.
- Bulk retries are capped (``MAX_BULK_RETRY``) and need confirmation
  outside ``--dry-run``.
- The retry enqueues through the normal worker path, so ``RELAY_DRY_RUN``
  enforcement downstream still applies; callers surface whether it is on.
"""

from dataclasses import dataclass, field

from django.db import transaction

from mailing.dry_run import dry_run_enforced
from mailing.models import (
    BlockedSender,
    EmailEventType,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.contacts import is_transactional_email_allowed, normalize_email
from mailing.services.operator_management import audit

MAX_BULK_RETRY = 100

RETRY_ACTION = "transactional_message.retry"

NON_FORCE_RETRYABLE_STATUSES = frozenset({TransactionalMessageStatus.FAILED})


class TransactionalRetryError(Exception):
    """Base error with a machine-readable code for API/CLI mapping."""

    def __init__(self, code, message="", *, status_code=409):
        self.code = code
        self.status_code = status_code
        super().__init__(message or code)


class MessageNotRetryable(TransactionalRetryError):
    def __init__(self, status):
        super().__init__(
            "message_not_retryable",
            f"message status is {status}; only failed messages retry without --force",
            status_code=409,
        )


class RetryReasonRequired(TransactionalRetryError):
    def __init__(self):
        super().__init__(
            "reason_required",
            "--force retries require a reason",
            status_code=400,
        )


class RetrySuppressed(TransactionalRetryError):
    def __init__(self, reason):
        super().__init__(
            "recipient_suppressed",
            f"recipient is suppressed/blocked ({reason}); retry refused",
            status_code=409,
        )
        self.suppression_reason = reason


def recipient_block_reason(email):
    """Return a block reason if the recipient hits the BlockedSender list."""
    normalized = normalize_email(email or "")
    if not normalized:
        return ""
    if BlockedSender.objects.filter(
        scope=BlockedSender.Scope.ADDRESS, value__iexact=normalized
    ).exists():
        return "blocked_address"
    domain = normalized.partition("@")[2]
    if domain and BlockedSender.objects.filter(
        scope=BlockedSender.Scope.DOMAIN, value__iexact=domain
    ).exists():
        return "blocked_domain"
    return ""


def suppression_block_reason(message):
    """Return a reason string when the message must never retry, else ''."""
    contact = message.contact
    if contact is not None and not is_transactional_email_allowed(contact):
        if getattr(contact, "hard_bounced_at", None) is not None:
            return "hard_bounce"
        if getattr(contact, "complained_at", None) is not None:
            return "complaint"
        return "suppressed"
    return recipient_block_reason(message.email)


def resolve_retry_sender(message):
    """Re-resolve the sender at retry time so a fixed route applies.

    Returns ``(sender_id, email, changed)``. Falls back to the stored
    sender when the client no longer has a matching sender configured,
    so a sender-config problem never blocks the re-queue itself; the
    worker then resolves the SES route from the (possibly updated)
    ``from_email`` at send time.
    """
    from mailing.services.senders import resolve_sender_email  # noqa: PLC0415 - avoid import cycle

    client = message.client
    candidates = [
        message.from_email_id,
        getattr(message.template, "default_sender_id", ""),
        client.default_sender_id,
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            sender = resolve_sender_email(client, candidate)
        except Exception:
            continue
        changed = sender.email != (message.from_email or "") or sender.sender_id != (
            message.from_email_id or ""
        )
        return sender.sender_id, sender.email, changed
    return message.from_email_id, message.from_email, False


@dataclass(frozen=True)
class RetryResult:
    message_id: int
    status: str
    enqueued: bool
    retry_count: int
    sender_changed: bool
    dry_run_enforced: bool


@dataclass
class BulkRetryResult:
    retried: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    dry_run: bool = False
    dry_run_enforced: bool = False

    def to_dict(self):
        return {
            "retried": self.retried,
            "skipped": self.skipped,
            "retried_count": len(self.retried),
            "skipped_count": len(self.skipped),
            "dry_run": self.dry_run,
            "dry_run_enforced": self.dry_run_enforced,
        }


def _next_retry_count(message):
    try:
        return int((message.metadata or {}).get("retry_count", 0)) + 1
    except (TypeError, ValueError):
        return 1


@transaction.atomic
def retry_transactional_message(message_or_id, *, actor=None, reason="", force=False):
    """Retry one transactional message, keeping its row/idempotency key."""
    from mailing.enqueue import enqueue_transactional_email  # noqa: PLC0415 - avoid import cycle
    from mailing.services.transactional import (  # noqa: PLC0415 - avoid import cycle
        append_transactional_event,
        build_transactional_queue_payload,
    )

    if isinstance(message_or_id, TransactionalMessage):
        message = (
            TransactionalMessage.objects.select_for_update()
            .select_related("client", "contact", "template")
            .get(pk=message_or_id.pk)
        )
    else:
        message = (
            TransactionalMessage.objects.select_for_update()
            .select_related("client", "contact", "template")
            .get(pk=message_or_id)
        )

    prior_status = message.status
    prior_error = message.last_error or ""

    if prior_status not in NON_FORCE_RETRYABLE_STATUSES and not force:
        raise MessageNotRetryable(prior_status)
    if force and not (reason or "").strip():
        raise RetryReasonRequired()

    blocked = suppression_block_reason(message)
    if blocked:
        raise RetrySuppressed(blocked)

    sender_id, sender_email, sender_changed = resolve_retry_sender(message)
    retry_count = _next_retry_count(message)
    enforced = dry_run_enforced()

    message.from_email_id = sender_id
    message.from_email = sender_email
    message.status = TransactionalMessageStatus.QUEUED
    message.last_error = ""
    metadata = dict(message.metadata or {})
    metadata["retry_count"] = retry_count
    if (reason or "").strip():
        metadata["last_retry_reason"] = (reason or "").strip()[:500]
    message.metadata = metadata
    message.save(update_fields=["from_email_id", "from_email", "status", "last_error", "metadata", "updated_at"])

    append_transactional_event(
        message,
        EmailEventType.QUEUED,
        {
            "retry": True,
            "retry_count": retry_count,
            "prior_status": prior_status,
            "prior_error": prior_error[:2000],
            "reason": (reason or "").strip()[:500],
            "sender_changed": sender_changed,
        },
    )

    queue_payload = build_transactional_queue_payload(message)
    transaction.on_commit(lambda: enqueue_transactional_email(queue_payload))

    audit(
        actor,
        RETRY_ACTION,
        message,
        {
            "prior_status": prior_status,
            "prior_error": prior_error[:500],
            "reason": (reason or "").strip()[:500],
            "force": bool(force),
            "retry_count": retry_count,
            "sender_changed": sender_changed,
            "from_email": sender_email,
            "dry_run_enforced": enforced,
        },
    )
    message.refresh_from_db()
    return RetryResult(
        message_id=message.id,
        status=message.status,
        enqueued=True,
        retry_count=retry_count,
        sender_changed=sender_changed,
        dry_run_enforced=enforced,
    )


def collect_retry_candidates(
    *,
    client=None,
    client_slug="",
    status=TransactionalMessageStatus.FAILED,
    created_after=None,
    created_before=None,
    error_contains="",
    template_key="",
):
    """Build the bulk-retry queryset (oldest first, capped by the caller)."""
    qs = TransactionalMessage.objects.select_related("client", "contact", "template").order_by(
        "created_at", "id"
    )
    if client is not None:
        qs = qs.filter(client=client)
    if client_slug:
        qs = qs.filter(client__slug=client_slug)
    if status:
        qs = qs.filter(status=status)
    if created_after is not None:
        qs = qs.filter(created_at__gte=created_after)
    if created_before is not None:
        qs = qs.filter(created_at__lte=created_before)
    if error_contains:
        qs = qs.filter(last_error__icontains=error_contains)
    if template_key:
        qs = qs.filter(template_key=template_key)
    return qs


def bulk_retry_transactional_messages(
    queryset, *, actor=None, reason="", force=False, limit=MAX_BULK_RETRY, dry_run=False
):
    """Bulk-retry a candidate queryset with per-row guardrails.

    Never raises for a skippable row: suppressed/blocked and (without
    force) non-failed rows are reported in ``skipped`` instead.
    """
    enforced = dry_run_enforced()
    result = BulkRetryResult(dry_run=dry_run, dry_run_enforced=enforced)
    if dry_run:
        for message in queryset[:limit]:
            result.retried.append(
                {
                    "id": message.id,
                    "email": message.email,
                    "client": message.client.slug if message.client_id else "",
                    "template_key": message.template_key,
                    "status": message.status,
                    "last_error": (message.last_error or "")[:300],
                }
            )
        return result
    for message in queryset[:limit]:
        try:
            retry = retry_transactional_message(message.pk, actor=actor, reason=reason, force=force)
        except TransactionalRetryError as exc:
            result.skipped.append({"id": message.id, "email": message.email, "code": exc.code})
        else:
            result.retried.append(
                {"id": retry.message_id, "email": message.email, "retry_count": retry.retry_count}
            )
    return result
