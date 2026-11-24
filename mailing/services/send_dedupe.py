"""Cross-sender duplicate-send guard: one (client, address, rendered-content hash) is sent once.

Every campaign and transactional send reserves its fingerprint row *before* the SES call.
The reservation is a plain INSERT guarded by the unique index
(``INSERT .. ON CONFLICT DO NOTHING``), so two senders racing on the same content cannot
both win. The loser is recorded as ``duplicate_blocked`` and never reaches SES.

What is deliberately NOT a duplicate:
  * the owner itself (a redelivered/retried message re-reserves its own row);
  * a row whose send definitely did not happen (state ``released``);
  * a ``sent`` row older than the policy window (campaign 30d, transactional 15m, per-template);
  * a send covered by an unexpired, audited ``SendDedupeOverride``.

An ambiguous SES outcome keeps the row ``reserved`` forever: same doctrine as the campaign
UNKNOWN status, never auto-resent.
"""

import functools
import hashlib
import json
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from mailing.models import (
    CampaignTaskAlert,
    SendDedupeOverride,
    SendFingerprint,
    SendFingerprintState,
)

logger = logging.getLogger(__name__)

# Per-recipient material that must not influence the hash: open pixel, click redirect
# (token + HMAC signature), unsubscribe link. The click destination stays in the hash.
_OPEN_PIXEL_RE = re.compile(r"/t/o/[A-Za-z0-9_-]+\.gif")
_CLICK_RE = re.compile(r"/t/c/[A-Za-z0-9_-]+\?u=")
_SIGNATURE_RE = re.compile(r"(&amp;|&)s=[0-9a-f]{64}")
_UNSUBSCRIBE_RE = re.compile(r"/unsubscribe/[A-Za-z0-9_-]+")
_WHITESPACE_RE = re.compile(r"\s+")
_NAMESPACE = uuid.UUID("5d1c4f0e-3a41-4c43-8f0a-6a6c0f7d6a11")


def dedupe_enabled():
    return bool(getattr(settings, "DEDUPE_ENABLED", True))


def normalise_text(value):
    value = value or ""
    if len(value) >= _NORMALISE_CACHE_MIN_CHARS:
        # Every recipient of a campaign without merge fields hashes the same subject and
        # bodies: normalising a newsletter body was ~10% of sender CPU (py-spy, 2026-11-24).
        return _normalise_text_cached(value)
    return _normalise_text(value)


_NORMALISE_CACHE_MIN_CHARS = 256


@functools.lru_cache(maxsize=16)
def _normalise_text_cached(value):
    return _normalise_text(value)


def _normalise_text(value):
    value = unicodedata.normalize("NFC", value)
    value = _OPEN_PIXEL_RE.sub("/t/o/TOKEN.gif", value)
    value = _CLICK_RE.sub("/t/c/TOKEN?u=", value)
    value = _SIGNATURE_RE.sub("&s=SIG", value)
    value = _UNSUBSCRIBE_RE.sub("/unsubscribe/TOKEN", value)
    return _WHITESPACE_RE.sub(" ", value).strip()


def content_hash(*parts):
    """Stable SHA-256 over normalised parts (subject, html, text, ...)."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(normalise_text(part).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def normalise_email(email):
    return (email or "").strip().lower()


@dataclass(frozen=True)
class Candidate:
    key: object  # caller's handle: recipient pk / message pk
    email: str
    content_hash: str


@dataclass(frozen=True)
class Blocked:
    content_hash: str
    first_state: str
    first_at: object
    first_campaign_recipient_id: int | None
    first_transactional_message_id: int | None


def _owner_filter(kind):
    return "campaign_recipient_id" if kind == "campaign" else "transactional_message_id"


def _active_overrides(client_id, campaign_id, now):
    """Overrides are always narrow: one campaign, or one address + content hash. Never client-wide."""
    scope = Q(~Q(email="") & ~Q(content_hash=""))
    if campaign_id:
        scope |= Q(campaign_id=campaign_id)
    return list(SendDedupeOverride.objects.filter(client_id=client_id, expires_at__gt=now).filter(scope))


def _override_for(overrides, campaign_id, email, chash):
    for override in overrides:
        if override.campaign_id and override.campaign_id == campaign_id:
            return override
        if override.email and override.email == email and override.content_hash == chash:
            return override
    return None


def reserve(client_id, candidates, *, kind, window_seconds, campaign_id=None):
    """Atomically reserve each candidate; returns ``(reserved_keys, {key: Blocked})``.

    ``kind`` is ``"campaign"`` (key = CampaignRecipient pk) or ``"transactional"``
    (key = TransactionalMessage pk). ``window_seconds=None`` means forever.
    """
    if not candidates:
        return [], {}
    now = timezone.now()
    owner_field = _owner_filter(kind)
    rows = [
        SendFingerprint(
            client_id=client_id, email=normalise_email(c.email), content_hash=c.content_hash,
            state=SendFingerprintState.RESERVED, reserved_at=now, **{owner_field: c.key},
        )
        for c in candidates
    ]
    SendFingerprint.objects.bulk_create(rows, ignore_conflicts=True, batch_size=500)
    existing = {
        (row.email, row.content_hash): row
        for row in SendFingerprint.objects.filter(
            client_id=client_id,
            email__in={normalise_email(c.email) for c in candidates},
            content_hash__in={c.content_hash for c in candidates},
        )
    }
    cutoff = None if window_seconds is None else now - timedelta(seconds=window_seconds)
    overrides = None
    reserved, blocked = [], {}
    for cand in candidates:
        email = normalise_email(cand.email)
        row = existing.get((email, cand.content_hash))
        if row is None:  # purged between insert and read: treat as free, next send re-reserves
            reserved.append(cand.key)
            continue
        if getattr(row, owner_field) == cand.key and row.state != SendFingerprintState.SENT:
            if row.state == SendFingerprintState.RELEASED:
                _take_over(row, owner_field, cand.key, now)
            reserved.append(cand.key)
            continue
        takeover_override = None
        takeover = row.state == SendFingerprintState.RELEASED or (
            row.state == SendFingerprintState.SENT
            and cutoff is not None
            and (row.sent_at or row.reserved_at) < cutoff
        )
        if not takeover:
            if overrides is None:
                overrides = _active_overrides(client_id, campaign_id, now)
            takeover_override = _override_for(overrides, campaign_id, email, cand.content_hash)
            takeover = takeover_override is not None
        if takeover and _take_over(row, owner_field, cand.key, now, override=takeover_override):
            reserved.append(cand.key)
            continue
        blocked[cand.key] = Blocked(
            content_hash=cand.content_hash, first_state=row.state, first_at=row.sent_at or row.reserved_at,
            first_campaign_recipient_id=row.campaign_recipient_id,
            first_transactional_message_id=row.transactional_message_id,
        )
    return reserved, blocked


def _take_over(row, owner_field, key, now, *, override=None):
    """Compare-and-swap the row to a new owner; exactly one racer wins."""
    fields = {"campaign_recipient_id": None, "transactional_message_id": None, owner_field: key}
    updated = SendFingerprint.objects.filter(pk=row.pk, state=row.state, reserved_at=row.reserved_at).update(
        state=SendFingerprintState.RESERVED, reserved_at=now, sent_at=None,
        override_id=override.pk if override else None, **fields,
    )
    if not updated:
        return False
    if override is not None:
        SendDedupeOverride.objects.filter(pk=override.pk).update(uses=F("uses") + 1)
        logger.warning(
            "send_dedupe_override_used override_id=%s created_by=%s client_id=%s", override.pk,
            override.created_by, override.client_id,
        )
    return True


def mark_sent(kind, keys):
    keys = list(keys)
    if keys:
        SendFingerprint.objects.filter(
            **{f"{_owner_filter(kind)}__in": keys}, state=SendFingerprintState.RESERVED
        ).update(state=SendFingerprintState.SENT, sent_at=timezone.now())


def release(kind, keys):
    """SES definitely did not send: free the slot so a legitimate retry/resend can proceed."""
    keys = list(keys)
    if keys:
        SendFingerprint.objects.filter(
            **{f"{_owner_filter(kind)}__in": keys}, state=SendFingerprintState.RESERVED
        ).update(state=SendFingerprintState.RELEASED)


def release_sent(kind, key):
    """A bounce means the copy never reached the mailbox: a re-send of the same content is legitimate."""
    SendFingerprint.objects.filter(**{_owner_filter(kind): key}).update(state=SendFingerprintState.RELEASED)


def campaign_window_seconds():
    return getattr(settings, "DEDUPE_CAMPAIGN_WINDOW_SECONDS", 30 * 86400)


def transactional_window_seconds(template):
    configured = getattr(template, "dedupe_window_seconds", None)
    if configured is None:
        return getattr(settings, "DEDUPE_TRANSACTIONAL_WINDOW_SECONDS", 900)
    return configured  # 0 = guard off for this template


def alert_campaign_blocked(campaign_id, count):
    """One durable, visible alert per campaign (updated in place), plus an ERROR log line."""
    logger.error("send_duplicate_blocked campaign_id=%s blocked=%d", campaign_id, count)
    task_id = uuid.uuid5(_NAMESPACE, f"duplicate_blocked:{campaign_id}")
    alert, created = CampaignTaskAlert.objects.get_or_create(
        task_id=task_id, defaults={"campaign_id": campaign_id, "message": ""},
    )
    total = count
    if not created and alert.message.startswith("duplicate_blocked:"):
        try:
            total += int(alert.message.split(":", 1)[1].split()[0])
        except (ValueError, IndexError):
            pass
    alert.message = (
        f"duplicate_blocked: {total} recipient(s) skipped because identical content was already "
        "sent to them (cross-campaign duplicate guard)."
    )
    alert.save(update_fields=["message"])


def campaign_content_hash(campaign, merge_values, recipient_context):
    """Content hash for one campaign recipient, computed without per-recipient tokens."""
    from mailing.services.campaign_merge import substitute, with_sentinels  # noqa: PLC0415

    if campaign.template_key:
        context = dict(campaign.context or {})
        if isinstance(recipient_context, dict):
            context.update(recipient_context)
        context.pop("unsubscribe_url", None)
        return content_hash(
            campaign.template_key, str(campaign.template_version or ""),
            json.dumps(context, sort_keys=True, default=str), campaign.subject,
            campaign.html_body, campaign.text_body,
        )
    html, text = with_sentinels(campaign.html_body, campaign.text_body)
    if isinstance(merge_values, dict):
        try:
            html = substitute(html, merge_values)
            text = substitute(text, merge_values, html_escape_html=False)
        except Exception:  # noqa: BLE001 - invalid merge values fail the recipient later; hash what we have
            pass
    return content_hash(campaign.subject or "", html, text)
