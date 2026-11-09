import logging
import queue
import statistics
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from mailing.aws import ses_client as default_ses_client
from mailing.models import (
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    EmailEvent,
    EmailEventType,
)
from mailing.services.api_errors import ApiValidationError
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
from mailing.services.tokens import (
    CampaignRecipientTokens,
    ensure_campaign_recipient_tokens,
    generate_raw_token,
    token_hash,
)
from mailing.ses import send_email, send_phase_timings

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


# A batch that delivers at less than this share of RELAY_SES_MAX_SEND_RATE is
# logged at ERROR. Batches smaller than the minimum are too short to measure
# and are not reported at all.
THROUGHPUT_ALERT_RATIO = 0.5
THROUGHPUT_ALERT_MIN_MESSAGES = 20


class CampaignSenderError(Exception):
    pass


class RetryableCampaignSendError(CampaignSenderError):
    pass


@dataclass(frozen=True)
class CampaignSendResult:
    sent_count: int
    skipped_count: int
    failed_count: int


def render_campaign_message(
    campaign, *, tracking_token="preview-tracking-token", unsubscribe_token="preview-unsubscribe-token"
):
    return {
        "subject": campaign.subject,
        "preview_text": campaign.preview_text,
        "html_body": build_campaign_html_body(campaign.html_body, tracking_token, unsubscribe_token),
        "text_body": build_campaign_text_body(campaign.text_body, unsubscribe_token),
    }


def send_campaign_test_message(campaign, to_email, *, ses_client=None, source=None):
    rendered = render_campaign_message(
        campaign,
        tracking_token=generate_raw_token(),
        unsubscribe_token=generate_raw_token(),
    )
    return send_email(
        ses_client=ses_client or default_ses_client(),
        source=source or _campaign_default_source(campaign) or settings.DEFAULT_FROM_EMAIL,
        to_email=to_email,
        subject=rendered["subject"],
        html_body=rendered["html_body"],
        text_body=rendered["text_body"],
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
        return resolve_sender_email(client).email
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


SEND_PHASES = ("lock_sql", "render_mime", "limiter_wait", "ses_call", "commit")


def send_campaign_batch(payload, *, ses_client=None):
    campaign_id = payload["campaign_id"]
    recipient_ids = payload["campaign_recipient_ids"]
    ses = ses_client or default_ses_client()
    result = {"sent_count": 0, "skipped_count": 0, "failed_count": 0}

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

    _mark_campaign_sending(campaign_id, recipient_ids)
    context = _BatchContext(campaign_id)
    concurrency = _send_concurrency(len(recipient_ids))
    started = time.monotonic()
    cpu_started = time.process_time()
    outcomes, failure = _send_recipients(campaign_id, recipient_ids, ses, concurrency, context)
    for outcome in outcomes:
        result[f"{outcome}_count"] += 1
    _report_batch_throughput(
        campaign_id, result, time.monotonic() - started, concurrency, context.timings, time.process_time() - cpu_started
    )

    if failure is not None:
        recipient_id, exc = failure
        if isinstance(exc, RetryableCampaignSendError):
            _record_retryable_error(campaign_id, recipient_id, str(exc))
            refresh_campaign_send_counts(campaign_id)
        raise exc

    refresh_campaign_send_counts(campaign_id)
    return CampaignSendResult(**result)


def _send_concurrency(recipient_count):
    if connection.vendor != "postgresql":
        # SQLite serializes writers (and the send transaction holds the write
        # lock), so threads would only add "database is locked" failures.
        return 1
    configured = int(getattr(settings, "CAMPAIGN_SEND_CONCURRENCY", 1) or 1)
    return max(1, min(configured, recipient_count))


def _send_recipients(campaign_id, recipient_ids, ses, concurrency, context):
    """Send each recipient once; return (outcomes, first_failure).

    The SES call is a synchronous network round trip of roughly 200 ms, so one
    thread tops out near 5 messages/s no matter how high the configured rate
    is. Recipients of a batch are independent (each is guarded by its own row
    lock), so a small pool of threads keeps enough calls in flight to reach it.
    The first error stops further sends; calls already in flight finish.
    """
    outcomes = []
    failures = []
    if concurrency == 1:
        for recipient_id in recipient_ids:
            try:
                outcomes.append(_send_campaign_recipient(campaign_id, recipient_id, ses, context))
            except Exception as exc:
                return outcomes, (recipient_id, exc)
        return outcomes, None

    pending = queue.SimpleQueue()
    for recipient_id in recipient_ids:
        pending.put(recipient_id)
    stop = threading.Event()

    def drain():
        # The pool threads (and their database connections) outlive the batch;
        # close_old_connections drops only broken or CONN_MAX_AGE-expired ones.
        try:
            while not stop.is_set():
                try:
                    recipient_id = pending.get_nowait()
                except queue.Empty:
                    return
                try:
                    outcomes.append(_send_campaign_recipient(campaign_id, recipient_id, ses, context))
                except Exception as exc:
                    failures.append((recipient_id, exc))
                    stop.set()
        finally:
            close_old_connections()

    # Sized by the configured maximum, not this batch's thread count, so a short
    # final batch does not resize (and reconnect) the pool.
    pool = _send_pool(max(1, int(getattr(settings, "CAMPAIGN_SEND_CONCURRENCY", 1) or 1)))
    futures = [pool.submit(drain) for _ in range(concurrency)]
    for future in futures:
        future.result()
    return outcomes, (failures[0] if failures else None)


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
    closers = [pool.submit(connection.close) for _ in range(pool._max_workers)]
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
    configured = float(getattr(settings, "SES_MAX_SEND_RATE_PER_SECOND", 0) or 0)
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

    Per-recipient sends only share-lock the campaign row, so none of them may
    update it: concurrent updaters holding that lock would deadlock.
    """
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update().get(pk=campaign_id)
        if campaign.status not in {CampaignStatus.QUEUED, CampaignStatus.FAILED}:
            return
        if campaign.scheduled_at and campaign.scheduled_at > timezone.now():
            return
        if not CampaignRecipient.objects.filter(pk__in=recipient_ids, status=CampaignRecipientStatus.PENDING).exists():
            return
        campaign.status = CampaignStatus.SENDING
        campaign.save(update_fields=["status", "updated_at"])


def _lock_campaign_shared(campaign_id):
    # Cancellation and dispatch still exclude each other (cancel takes FOR UPDATE,
    # which waits for in-flight sends), but sends no longer exclude one another.
    if connection.vendor == "postgresql":
        table = connection.ops.quote_name(Campaign._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT 1 FROM {table} WHERE id = %s FOR SHARE", [campaign_id])  # noqa: S608
            if cursor.fetchone() is None:
                raise Campaign.DoesNotExist(f"campaign {campaign_id} does not exist")
    return Campaign.objects.only("id", "status", "scheduled_at").get(pk=campaign_id)


def _send_campaign_recipient(campaign_id, recipient_id, ses, context):
    timing = {}
    started = time.perf_counter()
    with transaction.atomic():
        outcome = _send_campaign_recipient_locked(campaign_id, recipient_id, ses, context, timing)
        body_done = time.perf_counter()
    timing["commit"] = time.perf_counter() - body_done
    # Everything in the transaction that is not render/limiter/SES is lock and SQL work.
    in_tx = body_done - started
    timing["lock_sql"] = max(0.0, in_tx - timing.get("render_mime", 0) - timing.get("limiter_wait", 0) - timing.get("ses_call", 0))
    if outcome in {"sent", "failed"}:
        context.timings.append(timing)
    return outcome


def _send_campaign_recipient_locked(campaign_id, recipient_id, ses, context, timing):
    # Cancellation and dispatch share this lock; cancellation cannot race an
    # in-flight provider call and claim no message was sent.
    campaign = _lock_campaign_shared(campaign_id)
    recipient = (
        CampaignRecipient.objects.select_for_update(of=("self",))
        .select_related("contact")
        .get(pk=recipient_id, campaign_id=campaign_id)
    )
    recipient.campaign = context.campaign

    if recipient.status in TERMINAL_STATUSES or recipient.ses_message_id:
        return "skipped"

    if recipient.status != CampaignRecipientStatus.PENDING:
        return "skipped"

    if campaign.status == CampaignStatus.CANCELLED:
        recipient.status = CampaignRecipientStatus.SKIPPED
        recipient.last_error = "campaign_cancelled"
        recipient.save(update_fields=["status", "last_error", "updated_at"])
        _create_campaign_event(recipient, EmailEventType.SKIPPED, metadata={"reason": "campaign_cancelled"})
        return "skipped"

    if campaign.status == CampaignStatus.SCHEDULED or (campaign.scheduled_at and campaign.scheduled_at > timezone.now()):
        raise RetryableCampaignSendError("Campaign send time has not arrived.")
    if campaign.status in {CampaignStatus.DRAFT, CampaignStatus.SNAPSHOTTING}:
        raise CampaignSenderError("Campaign has not been released for sending.")

    tokens = _send_tokens_for_pending_recipient(recipient)
    return _deliver_campaign_recipient(recipient, ses, tokens, context.source, timing)


# Rendering an ~80 KB newsletter (HTML parse + link rewrite) costs ~15 ms of
# pure-Python CPU per recipient. Threads share the GIL, so that alone caps a
# worker near 25 msg/s however many SES calls are in flight. Render once per
# distinct body with sentinel tokens and substitute the real tokens per recipient.
_TRACKING_SENTINEL = "RLYTRK0SENTINEL0TOKEN"
_UNSUBSCRIBE_SENTINEL = "RLYUNS0SENTINEL0TOKEN"
_TEMPLATE_CACHE_MAX = 4
_template_cache = {}


def _render_recipient_bodies(campaign, tokens):
    key = (campaign.html_body, campaign.text_body)
    template = _template_cache.get(key)
    if template is None:
        template = (
            build_campaign_html_body(campaign.html_body, _TRACKING_SENTINEL, _UNSUBSCRIBE_SENTINEL),
            build_campaign_text_body(campaign.text_body, _UNSUBSCRIBE_SENTINEL),
        )
        if len(_template_cache) >= _TEMPLATE_CACHE_MAX:
            _template_cache.clear()
        _template_cache[key] = template
    html, text = template
    return (
        html.replace(_TRACKING_SENTINEL, tokens.tracking_token).replace(_UNSUBSCRIBE_SENTINEL, tokens.unsubscribe_token),
        text.replace(_UNSUBSCRIBE_SENTINEL, tokens.unsubscribe_token),
    )


def _deliver_campaign_recipient(recipient, ses, tokens, source, timing):
    render_started = time.perf_counter()
    html_body, text_body = _render_recipient_bodies(recipient.campaign, tokens)
    timing["render_mime"] = time.perf_counter() - render_started
    send_phase_timings.build = send_phase_timings.limiter_wait = 0.0
    send_started = time.perf_counter()

    # SES clients can raise transport, service, or unexpected adapter errors;
    # preserve the catch-all delivery contract at this external boundary.
    try:
        message_id = send_email(
            ses_client=ses,
            source=source,
            to_email=recipient.email,
            subject=recipient.campaign.subject,
            html_body=html_body,
            text_body=text_body,
            headers=campaign_unsubscribe_headers(tokens.unsubscribe_token, recipient.campaign),
        )
        _record_send_phases(timing, time.perf_counter() - send_started)
    except Exception as exc:
        _record_send_phases(timing, time.perf_counter() - send_started)
        retryable = is_retryable_send_error(exc)
        _log_campaign_delivery_error(recipient, exc, retryable)
        if retryable:
            raise RetryableCampaignSendError(str(exc)) from exc
        _mark_recipient_failed(recipient, str(exc))
        return "failed"

    _complete_campaign_recipient(recipient, message_id)
    return "sent"


def _record_send_phases(timing, send_elapsed):
    # send_email total = MIME/parameter build + limiter wait + the SES call itself.
    build = getattr(send_phase_timings, "build", 0.0)
    limiter = getattr(send_phase_timings, "limiter_wait", 0.0)
    timing["render_mime"] += build
    timing["limiter_wait"] = limiter
    timing["ses_call"] = max(0.0, send_elapsed - build - limiter)


def _log_campaign_delivery_error(recipient, exc, retryable):
    # Copy only stack locations: provider text, chains, source lines, and locals
    # may contain recipient content or credentials. Bound the emitted stack.
    diagnostic = traceback.TracebackException(RuntimeError, RuntimeError("SES delivery error details omitted"), None)
    frames = []
    for frame, lineno in traceback.walk_tb(exc.__traceback__):
        frames.append((frame.f_code.co_filename, lineno, frame.f_code.co_name, ""))
    diagnostic.stack = traceback.StackSummary.from_list(frames[-10:])
    classification = "permanent"
    if retryable:
        classification = "retryable"
    logger.error(
        "Campaign delivery failed campaign_id=%s recipient_id=%s classification=%s\n%s",
        recipient.campaign_id,
        recipient.pk,
        classification,
        "".join(diagnostic.format(chain=False)),
    )


def _complete_campaign_recipient(recipient, message_id):
    now = timezone.now()
    recipient.status = CampaignRecipientStatus.SENT
    recipient.ses_message_id = message_id
    recipient.sent_at = now
    recipient.last_error = ""
    recipient.save(update_fields=["status", "ses_message_id", "sent_at", "last_error", "updated_at"])
    _create_campaign_event(recipient, EmailEventType.SENT, metadata={"ses_message_id": message_id})


def is_retryable_send_error(exc):
    if isinstance(exc, (TimeoutError, ConnectionError, BotoCoreError)):
        return True
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        return code in TRANSIENT_SES_ERROR_CODES or status_code >= 500
    return False


@transaction.atomic
def refresh_campaign_send_counts(campaign_id):
    campaign = Campaign.objects.select_for_update().get(pk=campaign_id)
    recipients = CampaignRecipient.objects.filter(campaign_id=campaign_id)
    sent_count = recipients.filter(status=CampaignRecipientStatus.SENT).count()
    failed_count = recipients.filter(status=CampaignRecipientStatus.FAILED).count()
    pending_exists = recipients.filter(status=CampaignRecipientStatus.PENDING).exists()
    updates = {"sent_count": sent_count, "updated_at": timezone.now()}
    if campaign.status in {CampaignStatus.QUEUED, CampaignStatus.SENDING, CampaignStatus.FAILED}:
        if not pending_exists:
            updates["status"] = CampaignStatus.FAILED if failed_count else CampaignStatus.SENT
            if sent_count:
                updates["sent_at"] = campaign.sent_at or timezone.now()
        elif sent_count:
            updates["status"] = CampaignStatus.SENDING
    Campaign.objects.filter(pk=campaign_id).update(**updates)
    return {"sent_count": sent_count, "failed_count": failed_count}


def _mark_recipient_failed(recipient, error_message):
    recipient.status = CampaignRecipientStatus.FAILED
    recipient.last_error = error_message[:2000]
    recipient.save(update_fields=["status", "last_error", "updated_at"])
    _create_campaign_event(recipient, EmailEventType.FAILED, metadata={"error": recipient.last_error})


def _send_tokens_for_pending_recipient(recipient):
    tokens = ensure_campaign_recipient_tokens(recipient, locked=True)
    if tokens.tracking_token and tokens.unsubscribe_token:
        return tokens

    # Raw tokens are intentionally not stored by #10. If a retry reaches a
    # pending, unsent recipient with only hashes, no delivered email can depend
    # on those URLs yet, so rotating the hashes preserves that security model
    # while making the transient SES failure retryable.
    tracking_token = generate_raw_token()
    unsubscribe_token = generate_raw_token()
    recipient.tracking_token_hash = token_hash(tracking_token)
    recipient.unsubscribe_token_hash = token_hash(unsubscribe_token)
    recipient.save(update_fields=["tracking_token_hash", "unsubscribe_token_hash", "updated_at"])
    return CampaignRecipientTokens(
        tracking_token=tracking_token,
        unsubscribe_token=unsubscribe_token,
    )


def _record_retryable_error(campaign_id, recipient_id, error_message):
    CampaignRecipient.objects.filter(
        pk=recipient_id,
        campaign_id=campaign_id,
        status=CampaignRecipientStatus.PENDING,
    ).update(last_error=error_message[:2000], updated_at=timezone.now())


def _create_campaign_event(recipient, event_type, *, metadata=None):
    return EmailEvent.objects.create(
        campaign=recipient.campaign,
        campaign_recipient=recipient,
        contact=recipient.contact,
        client=recipient.campaign.client,
        audience=recipient.campaign.audience,
        event_type=event_type,
        metadata=metadata or {},
    )
