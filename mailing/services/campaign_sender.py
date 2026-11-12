import logging
import os
import queue
import re
import signal
import statistics
import threading
import time
import traceback
import uuid
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from urllib.parse import unquote_plus

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    ParamValidationError,
)
from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.db.models import Q
from django.utils import timezone

from mailing.aws import ses_client as default_ses_client
from mailing.mime_template import (
    TRACKING_SENTINEL as _TRACKING_SENTINEL,
)
from mailing.mime_template import (
    UNSUBSCRIBE_SENTINEL as _UNSUBSCRIBE_SENTINEL,
)
from mailing.mime_template import (
    build_campaign_template,
    render_campaign_template,
)
from mailing.models import (
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    CategoryPreference,
    EmailEvent,
    EmailEventType,
    RecipientListMember,
    Subscription,
)
from mailing.services.api_errors import ApiValidationError
from mailing.services.campaign_merge import (
    MergeVarError,
    member_merge_values,
    member_metadata_by_contact,
    merge_var_names,
    substitute,
    with_sentinels,
)
from mailing.services.campaign_rendering import (
    build_campaign_html_body as build_campaign_html_body,
)
from mailing.services.campaign_rendering import (
    build_campaign_text_body as build_campaign_text_body,
)
from mailing.services.campaign_rendering import (
    campaign_unsubscribe_headers,
)
from mailing.services.campaign_rendering import (
    rewrite_html_links as rewrite_html_links,
)
from mailing.services.campaign_template_rendering import render_campaign_fields
from mailing.services.click_security import sign_click_redirect
from mailing.services.public_urls import unsubscribe_url
from mailing.services.suppression import suppression_skip_reason
from mailing.services.tokens import (
    CampaignRecipientTokens,
    generate_raw_token,
    token_hash,
)
from mailing.ses import send_email, send_phase_timings, send_raw_bytes
from mailing.ses_rate_limiter import TRANSACTIONAL, SesRateLimiterUnavailable

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {
    CampaignRecipientStatus.SENT,
    CampaignRecipientStatus.SKIPPED,
    CampaignRecipientStatus.FAILED,
    CampaignRecipientStatus.BOUNCED,
    CampaignRecipientStatus.COMPLAINED,
    CampaignRecipientStatus.UNSUBSCRIBED,
}

TRANSIENT_SES_ERROR_CODES = {
    "Throttling",
    "ThrottlingException",
    "TooManyRequestsException",
    "RequestTimeout",
    "RequestTimeoutException",
    "ServiceUnavailable",
    "ServiceUnavailableException",
    "InternalError",
    "InternalFailure",
}


# A batch that delivers at less than this share of RELAY_SES_ACCOUNT_MAX_SEND_RATE is
# logged at ERROR. Batches smaller than the minimum are too short to measure
# and are not reported at all.
THROUGHPUT_ALERT_RATIO = 0.5
THROUGHPUT_ALERT_MIN_MESSAGES = 20


class CampaignSenderError(Exception):
    pass


class RetryableCampaignSendError(CampaignSenderError):
    pass


class CampaignHalted(CampaignSenderError):
    """Control signal: the campaign was paused or stopped mid-batch.

    Not an error: the batch ends cleanly, remaining PENDING recipients are
    left untouched (resumable), and no further SES calls are made.
    """




def render_campaign_message(
    campaign, *, tracking_token="preview-tracking-token", unsubscribe_token="preview-unsubscribe-token",
    recipient=None,
):
    fields = render_campaign_fields(
        campaign,
        recipient,
        context_overrides={"unsubscribe_url": unsubscribe_url(unsubscribe_token, campaign.unsubscribe_url_template)},
    )
    return {
        "subject": fields["subject"],
        "preview_text": campaign.preview_text,
        "html_body": build_campaign_html_body(
            fields["html_body"], tracking_token, unsubscribe_token, campaign.unsubscribe_url_template
        ),
        "text_body": build_campaign_text_body(fields["text_body"], unsubscribe_token, campaign.unsubscribe_url_template),
    }


def _test_send_merge_values(campaign, to_email):
    """Merge values for a test send: the list member with that address, else fail loudly."""
    names = merge_var_names(campaign)
    if not names:
        return {}
    metadata = (
        RecipientListMember.objects.filter(
            recipient_list__audience_id=campaign.audience_id,
            recipient_list__client_id=campaign.client_id,
            recipient_list__key=campaign.recipient_list_key,
            active=True,
            email__iexact=to_email,
        )
        .values_list("metadata", flat=True)
        .first()
    )
    try:
        return member_merge_values(metadata, names)
    except MergeVarError as exc:
        logger.error("campaign_test_send_merge_vars_invalid campaign_id=%s error=%s", campaign.pk, exc)
        raise ApiValidationError(
            {"merge_vars": f"Test address must be an active member of the campaign's recipient list "
                           f"with merge vars set ({exc}); nothing was sent."}
        ) from exc


def send_campaign_test_message(campaign, to_email, *, ses_client=None, source=None):
    merge_values = _test_send_merge_values(campaign, to_email)
    tokens = CampaignRecipientTokens(generate_raw_token(), generate_raw_token())
    html_body, text_body = _render_recipient_bodies(campaign, tokens, merge_values)
    return send_email(
        ses_client=ses_client or default_ses_client(),
        source=source or _campaign_default_source(campaign) or settings.DEFAULT_FROM_EMAIL,
        to_email=to_email,
        subject=campaign.subject,
        html_body=html_body,
        text_body=text_body,
        kind=TRANSACTIONAL,
    )


def _campaign_default_source(campaign):
    """Prefer the campaign client's default sender for test and batch sends.

    Falls back to None (the caller then uses DEFAULT_FROM_EMAIL) when the
    client has no sender configured. Grandfathered senders stored directly
    on Client.sender_emails resolve here without a SendingDomain row.
    """
    from mailing.models import Client  # noqa: PLC0415 - avoid import cycle
    from mailing.services.senders import resolve_sender_email  # noqa: PLC0415 - avoid import cycle

    client = getattr(campaign, "client", None)
    if client is None and getattr(campaign, "client_id", None):
        client = Client.objects.filter(pk=campaign.client_id).first()
    if client is None:
        return None
    try:
        return resolve_sender_email(client, campaign.sender_id).email
    except ApiValidationError:
        return None


class _BatchContext:
    """Per-batch values every recipient of the batch shares.

    Loaded once: the campaign row carries the ~80 KB body, and the sender
    resolution costs queries, so reloading them for each of 500 recipients was
    measurable GIL-held CPU. Status/schedule are still re-read under the row
    lock for every recipient.
    """

    def __init__(self, campaign_id):
        self.campaign = Campaign.objects.select_related("client", "audience").get(pk=campaign_id)
        self.source = _campaign_default_source(self.campaign) or settings.DEFAULT_FROM_EMAIL
        # One dict of per-phase seconds per attempted recipient (list.append is
        # thread-safe); summarized into the batch log line.
        self.timings = []
        self.mime_template = None
        self.merge_names = merge_var_names(self.campaign)


SEND_PHASES = ("lock_sql", "render_mime", "limiter_wait", "ses_call", "commit")


class CampaignSendInterrupted(RetryableCampaignSendError):
    """SIGTERM drain: in-flight calls finished and flushed, unstarted rows are PENDING again.

    The task layer re-enqueues the remaining PENDING rows; the worker is about to exit.
    """


_shutdown = threading.Event()
_sigterm_installed_handler = None


def install_sigterm_drain():
    """Chain a SIGTERM handler that asks running batches to drain; idempotent, main thread only.

    The handler only sets a flag (no logging, no locks: it can interrupt anything). The
    previous handler (the task worker's own shutdown) still runs, so the worker stops
    polling and exits once the batch returns.
    """
    global _sigterm_installed_handler
    if threading.current_thread() is not threading.main_thread():
        return False
    current = signal.getsignal(signal.SIGTERM)
    if current is _sigterm_installed_handler:
        return True
    previous = current

    def handler(signum, frame):
        _shutdown.set()
        if callable(previous):
            previous(signum, frame)

    signal.signal(signal.SIGTERM, handler)
    _sigterm_installed_handler = handler
    return True


def reset_sigterm_drain():
    """Clear the drain flag (tests; a real process exits after draining)."""
    _shutdown.clear()


@dataclass(frozen=True)
class CampaignSendResult:
    sent_count: int
    skipped_count: int
    failed_count: int
    unknown_count: int = 0


_Gate = namedtuple("_Gate", "status halted scheduled_at")


def _send_gate(campaign_id):
    """One lock-free read before every SES call: status, halt flag, schedule."""
    row = Campaign.objects.filter(pk=campaign_id).values_list("status", "send_halted_at", "scheduled_at").first()
    if row is None:
        raise CampaignSenderError(f"campaign {campaign_id} does not exist")
    return _Gate(row[0], row[1] is not None, row[2])


def _check_gate_sendable(campaign_id, gate):
    if gate.status == CampaignStatus.SCHEDULED or (gate.scheduled_at and gate.scheduled_at > timezone.now()):
        raise RetryableCampaignSendError("Campaign send time has not arrived.")
    if gate.status in {CampaignStatus.DRAFT, CampaignStatus.SNAPSHOTTING}:
        raise CampaignSenderError("Campaign has not been released for sending.")


@dataclass
class _Done:
    recipient: object
    outcome: str  # sent | failed | unknown | skipped
    message_id: str = ""
    error: str = ""
    skip_reason: str = ""
    timing: dict | None = None
    exc: BaseException | None = None


class _Finalizer:
    """Buffers per-recipient outcomes and writes them in one short transaction.

    Flushed by whichever sender thread notices it is due: >= FLUSH_MAX buffered items or
    FLUSH_INTERVAL seconds since the last flush. A message SES accepted but not yet
    flushed when the process dies stays SENDING and becomes UNKNOWN at lease expiry.
    """

    FLUSH_MAX = 50
    FLUSH_INTERVAL = 1.0

    def __init__(self, campaign):
        self.campaign = campaign
        self.lock = threading.Lock()
        self.buffer = []
        self.last_flush = time.monotonic()
        self.flushes = 0
        self.closed = False

    def add(self, done):
        with self.lock:
            self.buffer.append(done)
            due = self.closed or len(self.buffer) >= self.FLUSH_MAX or self._interval_due()
        if due:
            self.flush()

    def flush_if_due(self):
        with self.lock:
            due = bool(self.buffer) and self._interval_due()
        if due:
            self.flush()

    def _interval_due(self):
        return time.monotonic() - self.last_flush >= self.FLUSH_INTERVAL

    def flush(self, *, final=False):
        with self.lock:
            items, self.buffer = self.buffer, []
            self.last_flush = time.monotonic()
        if not items:
            return
        attempts = 3 if final else 1
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                _write_outcomes(self.campaign, items)
            except Exception:
                logger.exception(
                    "campaign_finalize_flush_failed campaign_id=%s items=%d attempt=%d/%d",
                    self.campaign.pk, len(items), attempt, attempts,
                )
                if attempt < attempts:
                    time.sleep(1.0)
                    continue
                with self.lock:
                    self.buffer = items + self.buffer
                if final:
                    # The rows stay SENDING (-> UNKNOWN at lease expiry); the accepted ids are
                    # logged so the operator and reconciliation can match them by hand.
                    accepted = ",".join(f"{d.recipient.pk}={d.message_id}" for d in items if d.outcome == "sent")
                    logger.error(
                        "campaign_finalize_lost campaign_id=%s unflushed=%d accepted_recipient_message_ids=%s",
                        self.campaign.pk, len(items), accepted,
                    )
                    raise CampaignSenderError(
                        f"could not record {len(items)} send outcomes for campaign {self.campaign.pk}"
                    ) from None
                return
            share = (time.perf_counter() - started) / len(items)
            for done in items:
                if done.timing is not None:
                    done.timing["commit"] = share
            self.flushes += 1
            return

    def close(self):
        """Flush what is buffered; threads still running after this flush their own results."""
        self.closed = True
        self.flush(final=True)


def _write_outcomes(campaign, items):
    now = timezone.now()
    by_outcome = {}
    for done in items:
        by_outcome.setdefault(done.outcome, []).append(done)
    events = []
    with transaction.atomic():
        sent = by_outcome.get("sent", [])
        if sent:
            objs = []
            for done in sent:
                r = done.recipient
                objs.append(CampaignRecipient(
                    pk=r.pk, status=CampaignRecipientStatus.SENT, ses_message_id=done.message_id,
                    sent_at=now, last_error="", updated_at=now,
                ))
                events.append(_event(campaign, r, EmailEventType.SENT, {"ses_message_id": done.message_id}))
            # A SENT result also wins over an UNKNOWN produced by lease expiry mid-flush.
            matched = CampaignRecipient.objects.filter(
                status__in=[CampaignRecipientStatus.SENDING, CampaignRecipientStatus.UNKNOWN]
            ).bulk_update(objs, ["status", "ses_message_id", "sent_at", "last_error", "updated_at"])
            if matched != len(objs):
                logger.error(
                    "campaign_finalize_state_mismatch campaign_id=%s outcome=sent expected=%d updated=%d",
                    campaign.pk, len(objs), matched,
                )
        for outcome, status, event_type in (
            ("failed", CampaignRecipientStatus.FAILED, EmailEventType.FAILED),
            ("unknown", CampaignRecipientStatus.UNKNOWN, None),
            ("skipped", CampaignRecipientStatus.SKIPPED, EmailEventType.SKIPPED),
        ):
            group = by_outcome.get(outcome, [])
            if not group:
                continue
            objs = []
            for done in group:
                r = done.recipient
                fields = {"pk": r.pk, "status": status, "last_error": done.error[:2000], "updated_at": now}
                if outcome == "skipped":
                    fields["skip_reason"] = done.skip_reason
                objs.append(CampaignRecipient(**fields))
                if event_type == EmailEventType.FAILED:
                    events.append(_event(campaign, r, event_type, {"error": done.error[:2000]}))
                elif event_type == EmailEventType.SKIPPED:
                    metadata = {"reason": str(done.skip_reason or done.error)}
                    if done.error.startswith("suppressed_after_snapshot"):
                        metadata["suppressed_after_snapshot"] = True
                    events.append(_event(campaign, r, event_type, metadata))
            update_fields = ["status", "last_error", "updated_at"]
            if outcome == "skipped":
                update_fields += ["skip_reason", "tracking_token_hash", "unsubscribe_token_hash"]  # nothing was sent
            matched = CampaignRecipient.objects.filter(status=CampaignRecipientStatus.SENDING).bulk_update(
                objs, update_fields
            )
            if matched != len(objs):
                logger.error(
                    "campaign_finalize_state_mismatch campaign_id=%s outcome=%s expected=%d updated=%d",
                    campaign.pk, outcome, len(objs), matched,
                )
        if events:
            EmailEvent.objects.bulk_create(events)


def _event(campaign, recipient, event_type, metadata):
    return EmailEvent(
        campaign_id=campaign.pk, campaign_recipient_id=recipient.pk, contact_id=recipient.contact_id,
        client_id=campaign.client_id, audience_id=campaign.audience_id, event_type=event_type, metadata=metadata,
    )


def _release_claims(recipient_ids, owner, *, error="", skipped_reason=""):
    """Return claimed-but-not-sent rows to PENDING (tokens dropped: nothing carrying them went out)."""
    if not recipient_ids:
        return 0
    return CampaignRecipient.objects.filter(
        pk__in=list(recipient_ids), status=CampaignRecipientStatus.SENDING, claim_owner=owner,
    ).update(
        status=CampaignRecipientStatus.PENDING, claimed_at=None, claim_owner="",
        tracking_token_hash=None, unsubscribe_token_hash=None, last_error=error[:2000], updated_at=timezone.now(),
    )


def _claim_chunk(campaign_id, recipient_ids, owner):
    """PENDING -> SENDING for the ids, in one autocommit UPDATE; returns (rows, tokens).

    Tokens are minted here, once. A row that SES accepted keeps these hashes forever, so
    the delivered copy's links work even if the row ends UNKNOWN. Only rows this UPDATE
    actually won (matched by the unique owner) are returned.
    """
    now = timezone.now()
    tokens = {}
    objs = []
    for recipient_id in recipient_ids:
        pair = CampaignRecipientTokens(generate_raw_token(), generate_raw_token())
        tokens[recipient_id] = pair
        objs.append(CampaignRecipient(
            pk=recipient_id, status=CampaignRecipientStatus.SENDING, claimed_at=now, claim_owner=owner,
            tracking_token_hash=token_hash(pair.tracking_token),
            unsubscribe_token_hash=token_hash(pair.unsubscribe_token), last_error="", updated_at=now,
        ))
    CampaignRecipient.objects.filter(campaign_id=campaign_id, status=CampaignRecipientStatus.PENDING).bulk_update(
        objs,
        ["status", "claimed_at", "claim_owner", "tracking_token_hash", "unsubscribe_token_hash", "last_error",
         "updated_at"],
    )
    rows = list(
        CampaignRecipient.objects.filter(
            campaign_id=campaign_id, claim_owner=owner, status=CampaignRecipientStatus.SENDING
        ).select_related("contact")
    )
    return rows, tokens


def _suppression_reasons(rows, campaign):
    """Pre-SES suppression recheck for a claimed chunk: two queries for the whole chunk.

    The snapshot may be minutes old; an unsubscribe, hard bounce or complaint recorded
    after it must still block the send. Contacts come from the claim's select_related.
    """
    contact_ids = [row.contact_id for row in rows]
    subscriptions = {}
    for subscription in Subscription.objects.filter(
        contact_id__in=contact_ids, audience_id=campaign.audience_id
    ).filter(Q(client_id=campaign.client_id) | Q(client__isnull=True)):
        subscriptions.setdefault(subscription.contact_id, {})[subscription.client_id] = subscription
    opted_out = set()
    if campaign.category_tag:
        opted_out = set(
            CategoryPreference.objects.filter(
                contact_id__in=contact_ids, audience_id=campaign.audience_id, client_id=campaign.client_id,
                tag=campaign.category_tag, enabled=False,
            ).values_list("contact_id", flat=True)
        )
    reasons = {}
    for row in rows:
        by_scope = subscriptions.get(row.contact_id, {})
        reason = suppression_skip_reason(
            row.contact, by_scope.get(None), by_scope.get(campaign.client_id),
            category_disabled=row.contact_id in opted_out,
        )
        if reason:
            reasons[row.pk] = reason
    return reasons


class _BatchRun:
    """One batch: threads drain a queue of recipient ids in claim chunks."""

    def __init__(self, campaign_id, recipient_ids, ses, context, concurrency=1):
        self.campaign_id = campaign_id
        self.concurrency = max(1, concurrency)
        self.ses = ses
        self.context = context
        self.pending = queue.SimpleQueue()
        for recipient_id in recipient_ids:
            self.pending.put(recipient_id)
        self.stop = threading.Event()
        self.failures = []
        self.outcomes = []
        self.halted = False
        self.interrupted = False
        self.finalizer = _Finalizer(context.campaign)
        self.chunk = max(1, int(getattr(settings, "CAMPAIGN_CLAIM_CHUNK", 20) or 20))

    def _take(self):
        # Shrink chunks toward the tail so the last rows spread over all threads
        # instead of one thread sending a full chunk serially.
        size = max(1, min(self.chunk, -(-self.pending.qsize() // self.concurrency)))
        ids = []
        while len(ids) < size:
            try:
                ids.append(self.pending.get_nowait())
            except queue.Empty:
                break
        return ids

    def drain(self, *, close_connections):
        try:
            while not self.stop.is_set():
                if _shutdown.is_set():
                    self.interrupted = self.interrupted or not self.pending.empty()
                    return
                self.finalizer.flush_if_due()
                ids = self._take()
                if not ids:
                    return
                self._process_chunk(ids)
        except Exception as exc:
            self.failures.append((None, exc))
            self.stop.set()
        finally:
            if close_connections:
                close_old_connections()

    def _halt(self):
        logger.error("Campaign batch halted campaign_id=%s", self.campaign_id)
        self.halted = True
        self.stop.set()

    def _process_chunk(self, ids):
        gate_started = time.perf_counter()
        gate = _send_gate(self.campaign_id)
        _check_gate_sendable(self.campaign_id, gate)
        if gate.halted:
            self._halt()  # unclaimed rows simply stay PENDING (pause is resumable)
            return
        owner = f"{os.getpid()}-{uuid.uuid4().hex[:24]}"
        rows, tokens = _claim_chunk(self.campaign_id, ids, owner)
        for _ in range(len(ids) - len(rows)):
            self.outcomes.append("skipped")  # already claimed/terminal: not ours to send
        sql_share = (time.perf_counter() - gate_started) / max(1, len(rows))
        campaign = self.context.campaign
        for row in rows:
            row.campaign = campaign
        reasons = _suppression_reasons(rows, campaign) if rows else {}
        sendable = []
        for row in rows:
            reason = reasons.get(row.pk)
            if reason:
                logger.info(
                    "Campaign recipient suppressed after snapshot campaign_id=%s recipient_id=%s reason=%s",
                    self.campaign_id, row.pk, reason,
                )
                self._finish(_Done(row, "skipped", error=f"suppressed_after_snapshot:{reason}", skip_reason=reason))
            else:
                sendable.append(row)
        merge_values = self._merge_values(sendable)
        index = 0
        delivered = False
        try:
            for index, row in enumerate(sendable):  # noqa: B007 - index is read by the handler below
                delivered = False
                started = time.perf_counter()
                gate = _send_gate(self.campaign_id)  # halt/cancel check before EVERY SES call
                gate_seconds = time.perf_counter() - started
                if self.stop.is_set() or _shutdown.is_set() or gate.halted:
                    _release_claims([r.pk for r in sendable[index:]], owner)
                    if gate.halted:
                        self._halt()
                    elif _shutdown.is_set():
                        self.interrupted = True
                    return
                if gate.status == CampaignStatus.CANCELLED:
                    for rest_row in sendable[index:]:
                        self._finish(_Done(rest_row, "skipped", error="campaign_cancelled"))
                    return
                timing = {"lock_sql": sql_share + gate_seconds}
                result = _deliver_campaign_recipient(
                    row, self.ses, tokens[row.pk], self.context.source, timing, self.context.mime_template,
                    merge_values.get(row.pk),
                )
                delivered = True
                if result.outcome == "retry":
                    _release_claims([r.pk for r in sendable[index:]], owner, error=result.error)
                    retryable = RetryableCampaignSendError(result.error)
                    retryable.__cause__ = result.exc
                    self.failures.append((row.pk, retryable))
                    self.stop.set()
                    return
                result.timing = timing
                self._finish(result)
        except Exception:
            # An internal error (database, not SES) must not strand claimed rows in SENDING:
            # return the ones that were not sent; a row whose call already ran is left to
            # finalize/lease handling.
            rest = sendable[index + 1:] if delivered else sendable[index:]
            try:
                _release_claims([r.pk for r in rest], owner, error="released_after_internal_error")
            except Exception:
                logger.exception("campaign_release_failed campaign_id=%s rows=%d", self.campaign_id, len(rest))
            raise

    def _merge_values(self, rows):
        """One query per chunk: ``{recipient_id: values | MergeVarError}``."""
        names = self.context.merge_names
        if not names or not rows:
            return {}
        metadata = member_metadata_by_contact(self.context.campaign, {row.contact_id for row in rows})
        values = {}
        for row in rows:
            try:
                values[row.pk] = member_merge_values(metadata.get(row.contact_id), names)
            except MergeVarError as exc:
                values[row.pk] = exc
        return values

    def _finish(self, done):
        if done.outcome in {"sent", "failed"} and done.timing is not None:
            self.context.timings.append(done.timing)
        self.outcomes.append(done.outcome)
        self.finalizer.add(done)

    def run(self, concurrency):
        if concurrency == 1:
            self.drain(close_connections=False)
        else:
            # Sized by the configured maximum, not this batch's thread count, so a short
            # final batch does not resize (and reconnect) the pool.
            pool = _send_pool(max(1, int(getattr(settings, "CAMPAIGN_SEND_CONCURRENCY", 1) or 1)))
            futures = [pool.submit(self.drain, close_connections=True) for _ in range(concurrency)]
            self._wait(futures)
        self.finalizer.close()

    def _wait(self, futures):
        drain_deadline = None
        while True:
            done, not_done = wait(futures, timeout=0.2)
            if not not_done:
                return
            if _shutdown.is_set():
                drain_deadline = drain_deadline or time.monotonic() + float(
                    getattr(settings, "CAMPAIGN_SEND_DRAIN_SECONDS", 25)
                )
                if time.monotonic() > drain_deadline:
                    self.stop.set()
                    self.interrupted = True
                    logger.error(
                        "campaign_send_drain_timeout campaign_id=%s in_flight_threads=%d; "
                        "their rows stay SENDING and become UNKNOWN at lease expiry",
                        self.campaign_id, len(not_done),
                    )
                    return


def send_campaign_batch(payload, *, ses_client=None):
    campaign_id = payload["campaign_id"]
    recipient_ids = payload["campaign_recipient_ids"]
    ses = ses_client or default_ses_client()
    result = {"sent_count": 0, "skipped_count": 0, "failed_count": 0, "unknown_count": 0}

    if not Campaign.objects.filter(pk=campaign_id).exists():
        raise CampaignSenderError(f"campaign {campaign_id} does not exist")

    recipients = CampaignRecipient.objects.filter(pk__in=recipient_ids).only("id", "campaign_id")
    found_recipient_ids = {recipient.id for recipient in recipients}
    missing_recipient_ids = sorted(set(recipient_ids) - found_recipient_ids)
    wrong_campaign_ids = sorted(recipient.id for recipient in recipients if recipient.campaign_id != campaign_id)
    if missing_recipient_ids or wrong_campaign_ids:
        raise CampaignSenderError(
            f"invalid campaign recipient ids: missing={missing_recipient_ids}, wrong_campaign={wrong_campaign_ids}"
        )

    install_sigterm_drain()
    _mark_campaign_sending(campaign_id, recipient_ids)
    context = _BatchContext(campaign_id)
    context.mime_template = _build_batch_mime_template(context)
    concurrency = _send_concurrency(len(recipient_ids))
    started = time.monotonic()
    cpu_started = time.process_time()
    if _campaign_send_halted(campaign_id):
        # Halted before the first send: leave every PENDING recipient
        # untouched so pause is resumable and stop keeps them as-is.
        logger.error("Campaign batch halted before first send campaign_id=%s", campaign_id)
        refresh_campaign_send_counts(campaign_id)
        return CampaignSendResult(sent_count=0, skipped_count=0, failed_count=0)
    run = _BatchRun(campaign_id, recipient_ids, ses, context, concurrency)
    try:
        run.run(concurrency)
    finally:
        for outcome in run.outcomes:
            result[f"{outcome}_count"] += 1
    _report_batch_throughput(
        campaign_id, result, time.monotonic() - started, concurrency, context.timings, time.process_time() - cpu_started
    )
    if result["unknown_count"]:
        logger.error(
            "campaign_send_unknown campaign_id=%s unknown=%d (ambiguous SES outcome; never auto-resent, "
            "see the campaign's UNKNOWN recipients)", campaign_id, result["unknown_count"],
        )

    failure = run.failures[0] if run.failures else None
    if failure is not None and run.halted:
        failure = None  # halt wins over a concurrent failure: end cleanly with PENDING rows untouched
    if failure is not None:
        _, exc = failure
        refresh_campaign_send_counts(campaign_id)
        raise exc
    refresh_campaign_send_counts(campaign_id)
    if run.interrupted:
        logger.error(
            "campaign_send_interrupted campaign_id=%s sent=%d: SIGTERM drain, unstarted rows returned to PENDING",
            campaign_id, result["sent_count"],
        )
        raise CampaignSendInterrupted("campaign batch interrupted by SIGTERM")
    _reconcile_early_webhooks(campaign_id)
    return CampaignSendResult(**result)


def _reconcile_early_webhooks(campaign_id):
    """Correlate SES events that beat this batch's commit; never fails the batch."""
    from mailing.services.ses_webhooks import reconcile_uncorrelated_ses_events  # noqa: PLC0415 - import cycle

    try:
        reconcile_uncorrelated_ses_events(campaign_id=campaign_id)
    except Exception:  # noqa: BLE001 - sends are committed; a later batch or the task retries
        logger.exception("ses_reconcile_failed campaign_id=%s", campaign_id)


def _send_concurrency(recipient_count):
    if connection.vendor != "postgresql":
        # SQLite serializes writers (and the send transaction holds the write
        # lock), so threads would only add "database is locked" failures.
        return 1
    configured = int(getattr(settings, "CAMPAIGN_SEND_CONCURRENCY", 1) or 1)
    return max(1, min(configured, recipient_count))


_pool = None
_pool_size = 0
_pool_lock = threading.Lock()


def _send_pool(size):
    """Persistent sender threads: Django connections are per-thread, so reusing
    the threads keeps their connections open across batches."""
    global _pool, _pool_size
    with _pool_lock:
        if _pool is None or _pool_size != size:
            if _pool is not None:
                shutdown_send_pool()
            _pool = ThreadPoolExecutor(max_workers=size, thread_name_prefix="campaign-send")
            _pool_size = size
        return _pool


def shutdown_send_pool():
    """Stop the sender threads and close their connections (resize, tests)."""
    global _pool, _pool_size
    pool, _pool, _pool_size = _pool, None, 0
    if pool is None:
        return
    # The lambda resolves ``connection`` in the pool thread; a bound ``connection.close`` would
    # belong to the calling thread's connection and be refused as cross-thread use.
    closers = [pool.submit(lambda: connection.close()) for _ in range(pool._max_workers)]
    for closer in closers:
        closer.result(timeout=30)
    pool.shutdown(wait=True)


def _percentile(sorted_values, fraction):
    return sorted_values[min(len(sorted_values) - 1, int(fraction * len(sorted_values)))]


def _phase_summary(timings):
    """`phase=sum_ms/p50_ms/p95_ms` per phase; counts and timings only, no PII."""
    parts = []
    for phase in SEND_PHASES:
        values = sorted(timing.get(phase, 0.0) * 1000 for timing in timings)
        if not values:
            parts.append(f"{phase}_ms=0/0/0")
            continue
        parts.append(f"{phase}_ms={sum(values):.0f}/{statistics.median(values):.1f}/{_percentile(values, 0.95):.1f}")
    return " ".join(parts)


def _report_batch_throughput(campaign_id, result, elapsed, concurrency, timings=(), cpu_seconds=0.0):
    attempted = result["sent_count"] + result["failed_count"]
    if attempted < THROUGHPUT_ALERT_MIN_MESSAGES or elapsed <= 0:
        return
    rate = attempted / elapsed
    configured = float(getattr(settings, "SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND", 0) or 0)
    logger.info(
        "Campaign batch sent campaign_id=%s attempted=%s elapsed_s=%.1f rate_per_s=%.1f "
        "configured_rate_per_s=%.1f concurrency=%s emails=%s worker_cpu_s=%.2f cpu_ms_per_email=%.1f "
        "phases(sum/p50/p95)[%s]",
        campaign_id, attempted, elapsed, rate, configured, concurrency, len(timings), cpu_seconds,
        cpu_seconds * 1000 / attempted, _phase_summary(timings),
    )
    if configured > 0 and rate < configured * THROUGHPUT_ALERT_RATIO:
        logger.error(
            "campaign_send_throughput_low campaign_id=%s attempted=%s rate_per_s=%.1f configured_rate_per_s=%.1f "
            "concurrency=%s",
            campaign_id, attempted, rate, configured, concurrency,
        )


def _mark_campaign_sending(campaign_id, recipient_ids):
    """Move a queued campaign to SENDING once, before any recipient is sent.

    A conditional UPDATE, not a row lock: nothing in the send path locks the campaign row.
    """
    if not CampaignRecipient.objects.filter(pk__in=recipient_ids, status=CampaignRecipientStatus.PENDING).exists():
        return
    now = timezone.now()
    Campaign.objects.filter(
        pk=campaign_id, status__in={CampaignStatus.QUEUED, CampaignStatus.FAILED}
    ).filter(Q(scheduled_at__isnull=True) | Q(scheduled_at__lte=now)).update(
        status=CampaignStatus.SENDING, updated_at=now
    )


def _campaign_send_halted(campaign_id):
    """Plain, lock-free read of the pause/stop flag. Never takes a row lock."""
    return bool(Campaign.objects.filter(pk=campaign_id).values_list("send_halted_at", flat=True).first())


# Rendering an ~80 KB newsletter (HTML parse + link rewrite) costs ~15 ms of
# pure-Python CPU per recipient. Threads share the GIL, so that alone caps a
# worker near 25 msg/s however many SES calls are in flight. Render once per
# distinct body with sentinel tokens and substitute the real tokens per recipient.
# MIME is built once per batch on top of that (mailing.mime_template): per
# recipient only byte substitution remains.
_TEMPLATE_CACHE_MAX = 4
_template_cache = {}
_CACHED_CLICK_URL_RE = re.compile(
    r"(?P<prefix>/t/c/)(?P<tracking_token>[A-Za-z0-9_-]+)"
    r'\?u=(?P<destination>[^"&\s]+)&amp;s=[0-9a-f]{64}'
)
_CACHED_CLICK_URL_BYTES_RE = re.compile(
    rb"(?P<prefix>/t/c/)(?P<tracking_token>[A-Za-z0-9_-]+)"
    rb'\?u=(?P<destination>[^"&\s]+)&amp;s=[0-9a-f]{64}'
)


def _template_bodies(campaign):
    key = (campaign.html_body, campaign.text_body, campaign.unsubscribe_url_template)
    template = _template_cache.get(key)
    if template is None:
        html_body, text_body = with_sentinels(campaign.html_body, campaign.text_body)
        template = (
            build_campaign_html_body(
                html_body, _TRACKING_SENTINEL, _UNSUBSCRIBE_SENTINEL, campaign.unsubscribe_url_template
            ),
            build_campaign_text_body(text_body, _UNSUBSCRIBE_SENTINEL, campaign.unsubscribe_url_template),
        )
        if len(_template_cache) >= _TEMPLATE_CACHE_MAX:
            _template_cache.clear()
        _template_cache[key] = template
    return template


def _render_recipient_bodies(campaign, tokens, merge_values=None):
    html, text = _template_bodies(campaign)
    html = html.replace(_TRACKING_SENTINEL, tokens.tracking_token).replace(
        _UNSUBSCRIBE_SENTINEL,
        tokens.unsubscribe_token,
    )
    html = substitute(html, merge_values or {})
    html = _resign_cached_click_urls(html, tokens.tracking_token)
    return (
        html,
        substitute(text.replace(_UNSUBSCRIBE_SENTINEL, tokens.unsubscribe_token), merge_values or {}),
    )


def _resign_cached_click_urls(message, tracking_token):
    """Rebind cached click links to this recipient's substituted tracking token."""
    is_bytes = isinstance(message, bytes)
    pattern = _CACHED_CLICK_URL_BYTES_RE if is_bytes else _CACHED_CLICK_URL_RE

    def resign(match):
        cached_token = match.group("tracking_token")
        if is_bytes:
            cached_token = cached_token.decode("ascii")
        if cached_token != tracking_token:
            raise CampaignSenderError("Cached click URL has a different recipient tracking token.")
        destination = match.group("destination")
        if is_bytes:
            destination = destination.decode("ascii")
        destination_url = unquote_plus(destination)
        signature = sign_click_redirect(tracking_token, destination_url)
        if is_bytes:
            signature = signature.encode("ascii")
            return (
                match.group("prefix")
                + match.group("tracking_token")
                + b"?u="
                + match.group("destination")
                + b"&amp;s="
                + signature
            )
        return f"{match.group('prefix')}{cached_token}?u={destination}&amp;s={signature}"

    return pattern.sub(resign, message)


def _build_batch_mime_template(context):
    """Build the batch's MIME once; None means use the per-message builder.

    Disabled via RELAY_MIME_TEMPLATE_ENABLED=0. A build failure is loud (error
    log) but falls back to the per-message builder so a batch is never blocked
    by the optimization.
    """
    if context.campaign.template_key or not getattr(settings, "RELAY_MIME_TEMPLATE_ENABLED", True):
        return None
    try:
        html_template, text_template = _template_bodies(context.campaign)
        return build_campaign_template(
            source=context.source,
            subject=context.campaign.subject,
            html_body=html_template,
            text_body=text_template,
            headers=campaign_unsubscribe_headers(_UNSUBSCRIBE_SENTINEL, context.campaign),
        )
    except Exception:
        logger.exception(
            "mime_template_build_failed campaign_id=%s; falling back to per-message builder",
            context.campaign.id,
        )
        return None


_NOT_SENT_CODES = {
    "Throttling",
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailable",
    "ServiceUnavailableException",
}
_NOT_SENT_BOTO = (ConnectTimeoutError, EndpointConnectionError, NoCredentialsError, ConnectionRefusedError)


def classify_send_error(exc):
    """What an SES exception says about whether the message went out.

    ``retry``   definitely not sent (throttled, could not connect): back to PENDING.
    ``failed``  definitely rejected (4xx, bad parameters): FAILED.
    ``unknown`` ambiguous (read timeout, reset, 5xx, anything on the wire that may have
                been accepted): UNKNOWN, never auto-resent.
    """
    if isinstance(exc, SesRateLimiterUnavailable):
        return "retry"  # fail-closed limiter: the message never reached SES
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if code in _NOT_SENT_CODES:
            return "retry"
        if status_code >= 500 or code in TRANSIENT_SES_ERROR_CODES:
            return "unknown"
        return "failed"
    if isinstance(exc, _NOT_SENT_BOTO):
        return "retry"
    if isinstance(exc, ParamValidationError):
        return "failed"
    if isinstance(exc, (TimeoutError, ConnectionError, BotoCoreError)):
        return "unknown"
    return "failed"


def _deliver_campaign_recipient(recipient, ses, tokens, source, timing, mime_template=None, merge_values=None):
    """Render and send one claimed recipient. No database access and no open transaction.

    SES clients can raise transport, service, or unexpected adapter errors; preserve the
    catch-all delivery contract at this external boundary. Template byte substitution
    runs inside it too: an unsafe per-recipient value fails that recipient loudly, never
    the batch, and never sends.
    """
    send_started = None
    try:
        render_started = time.perf_counter()
        if isinstance(merge_values, MergeVarError):
            raise merge_values  # nothing sent: logged and recorded as failed below
        if mime_template is not None:
            raw_data = _resign_cached_click_urls(
                substitute(
                    render_campaign_template(
                        mime_template,
                        to_email=recipient.email,
                        tracking_token=tokens.tracking_token,
                        unsubscribe_token=tokens.unsubscribe_token,
                    ),
                    merge_values or {},
                ),
                tokens.tracking_token,
            )
            html_body = text_body = None
            rendered = None
        else:
            if recipient.campaign.template_key:
                rendered = render_campaign_message(
                    recipient.campaign,
                    tracking_token=tokens.tracking_token,
                    unsubscribe_token=tokens.unsubscribe_token,
                    recipient=recipient,
                )
                html_body, text_body = rendered["html_body"], rendered["text_body"]
            else:
                rendered = None
                html_body, text_body = _render_recipient_bodies(
                    recipient.campaign, tokens, merge_values
                )
            raw_data = None
        timing["render_mime"] = time.perf_counter() - render_started
        send_phase_timings.build = send_phase_timings.limiter_wait = 0.0
        send_started = time.perf_counter()
        if raw_data is not None:
            message_id = send_raw_bytes(
                ses_client=ses, source=source, to_email=recipient.email, raw_data=raw_data
            )
        else:
            message_id = send_email(
                ses_client=ses,
                source=source,
                to_email=recipient.email,
                subject=rendered["subject"] if rendered is not None else recipient.campaign.subject,
                html_body=html_body,
                text_body=text_body,
                headers=campaign_unsubscribe_headers(tokens.unsubscribe_token, recipient.campaign),
            )
        _record_send_phases(timing, time.perf_counter() - send_started)
    except Exception as exc:
        if send_started is not None:
            _record_send_phases(timing, time.perf_counter() - send_started)
            classification = classify_send_error(exc)
        else:
            classification = "failed"  # nothing was sent: render error
        _log_campaign_delivery_error(recipient, exc, classification)
        if classification == "unknown":
            return _Done(recipient, "unknown", error=f"send_outcome_unknown:{type(exc).__name__}")
        if classification == "retry":
            return _Done(recipient, "retry", error=str(exc), exc=exc)
        return _Done(recipient, "failed", error=str(exc))

    return _Done(recipient, "sent", message_id=message_id)


def _record_send_phases(timing, send_elapsed):
    # send_email total = MIME/parameter build + limiter wait + the SES call itself.
    build = getattr(send_phase_timings, "build", 0.0)
    limiter = getattr(send_phase_timings, "limiter_wait", 0.0)
    timing["render_mime"] += build
    timing["limiter_wait"] = limiter
    timing["ses_call"] = max(0.0, send_elapsed - build - limiter)



def _log_campaign_delivery_error(recipient, exc, classification):
    # Copy only stack locations: provider text, chains, source lines, and locals
    # may contain recipient content or credentials. Bound the emitted stack.
    diagnostic = traceback.TracebackException(RuntimeError, RuntimeError("SES delivery error details omitted"), None)
    frames = []
    for frame, lineno in traceback.walk_tb(exc.__traceback__):
        frames.append((frame.f_code.co_filename, lineno, frame.f_code.co_name, ""))
    diagnostic.stack = traceback.StackSummary.from_list(frames[-10:])
    label = {"retry": "retryable", "failed": "permanent", "unknown": "unknown"}[classification]
    logger.error(
        "Campaign delivery failed campaign_id=%s recipient_id=%s classification=%s\n%s",
        recipient.campaign_id,
        recipient.pk,
        label,
        "".join(diagnostic.format(chain=False)),
    )


def is_retryable_send_error(exc):
    if isinstance(exc, SesRateLimiterUnavailable):
        # Fail-closed limiter (DB down or permit wait timed out): the message
        # was never handed to SES, so a later retry cannot duplicate it.
        return True
    if isinstance(exc, (TimeoutError, ConnectionError, BotoCoreError)):
        return True
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        return code in TRANSIENT_SES_ERROR_CODES or status_code >= 500
    return False



_OPEN_CAMPAIGN_STATUSES = {CampaignStatus.QUEUED, CampaignStatus.SENDING, CampaignStatus.FAILED}


def refresh_campaign_send_counts(campaign_id):
    """Recount and settle the campaign status without locking the campaign row.

    Plain conditional UPDATEs only: the counts, then a status change guarded by
    ``status IN (queued, sending, failed)`` so a concurrent cancel is never overwritten.
    """
    recipients = CampaignRecipient.objects.filter(campaign_id=campaign_id)
    # SES-accepted, not status=SENT: a later bounce/complaint/unsubscribe moves
    # the status away from SENT but the message was still accepted by SES.
    sent_count = recipients.filter(sent_at__isnull=False).count()
    failed_count = recipients.filter(status=CampaignRecipientStatus.FAILED).count()
    # SENDING rows belong to a batch that is still running (or to a lease that has not expired).
    unfinished = recipients.filter(
        status__in=[CampaignRecipientStatus.PENDING, CampaignRecipientStatus.SENDING]
    ).exists()
    now = timezone.now()
    campaigns = Campaign.objects.filter(pk=campaign_id)
    campaigns.update(sent_count=sent_count, updated_at=now)
    open_campaigns = campaigns.filter(status__in=_OPEN_CAMPAIGN_STATUSES)
    if not unfinished:
        if sent_count:
            open_campaigns.filter(sent_at__isnull=True).update(sent_at=now)
        open_campaigns.update(status=CampaignStatus.FAILED if failed_count else CampaignStatus.SENT)
    elif sent_count:
        open_campaigns.update(status=CampaignStatus.SENDING)
    return {"sent_count": sent_count, "failed_count": failed_count}
