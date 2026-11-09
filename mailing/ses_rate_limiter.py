"""Global account-wide SES token bucket backed by one Postgres row per account/region.

Every SES send path (campaign batches, transactional mail, test sends,
monitor alerts) acquires a permit here via :func:`acquire_ses_permit`, so any
number of sender processes share a single budget instead of each pacing
locally. Permits are weighted by recipient count; the caller passes ``cost``.

Fail-closed: a database error raises :class:`SesRateLimiterUnavailable` (never
a silent send), and a wait past the configured timeout raises
:class:`SesRateLimiterTimeout`. Both are retryable at the task layer.

Throttling feedback (AIMD): :func:`report_ses_throttling` multiplies the
shared rate by 0.8 for 60 s; afterwards the rate recovers linearly to the
base rate over 60 s. The pure helpers (:func:`effective_rate`,
:func:`feedback_penalty`) take explicit timestamps so tests can use a fake
clock; the DB layer converts datetimes to epoch seconds for them.
"""

import logging
import time
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.utils import DatabaseError, IntegrityError
from django.utils import timezone

from mailing.models import SesRateLimitBucket

logger = logging.getLogger(__name__)

BULK = "bulk"
TRANSACTIONAL = "transactional"
KINDS = (BULK, TRANSACTIONAL)

THROTTLING_CODES = frozenset({
    "Throttling",
    "ThrottlingException",
    "ThrottledException",
    "RequestThrottled",
    "RequestLimitExceeded",
    "ThrottlingRateExceeded",
    "TooManyRequestsException",
    "BandwidthLimitExceeded",
    "PriorRequestNotComplete",
})

# Momentary lock contention is not an outage: another sender holds the bucket
# row (Postgres waits on the row lock; SQLite raises). Retry these until the
# permit deadline, which still fails closed via SesRateLimiterTimeout. Anything
# else (refused connection, dead socket, missing table) fails closed at once.
_LOCK_CONTENTION_MARKERS = (
    "database is locked",
    "database table is locked",
    "deadlock detected",
    "lock timeout",
    "could not obtain lock",
)


def _is_lock_contention(exc):
    return isinstance(exc, DatabaseError) and any(
        marker in str(exc).lower() for marker in _LOCK_CONTENTION_MARKERS
    )

# Cache for the best-effort SES MaxSendRate discovery (monotonic expiry, value).
_account_quota_cache = {"expires": 0.0, "value": None}
ACCOUNT_QUOTA_CACHE_SECONDS = 300.0


class SesRateLimiterError(RuntimeError):
    """Base class for limiter errors; never raised directly."""


class SesRateLimiterUnavailable(SesRateLimiterError):
    """The limiter's database is unavailable: fail closed, do not send."""


class SesRateLimiterTimeout(SesRateLimiterUnavailable):
    """No permit became available before the configured wait timeout."""


def bucket_key(*, region=None):
    """One bucket per SES account/region; routes without their own row share it.

    Distinct (role, region) pairs conservatively share the default row until a
    per-route key is threaded through, so the shared budget is never exceeded.
    """
    return f"ses-v1:{region or settings.AWS_SES_REGION}"


def limiter_config():
    """Snapshot of the limiter settings (re-read per call for override_settings)."""
    base_rate = max(0.0, float(getattr(settings, "SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND", 0) or 0))
    burst = float(getattr(settings, "SES_LIMITER_BURST_MESSAGES", 0) or 0)
    if burst <= 0:
        burst = base_rate / 5.0
    reserve = max(0.0, float(getattr(settings, "SES_TRANSACTIONAL_RESERVE_PER_SECOND", 0) or 0))
    if base_rate > 0 and reserve >= base_rate:
        logger.error(
            "ses_rate_limit_reserve_clamped reserve_per_s=%.1f base_rate_per_s=%.1f",
            reserve, base_rate,
        )
        reserve = max(0.0, base_rate - 1.0)
    timeout = max(0.0, float(getattr(settings, "SES_LIMITER_WAIT_TIMEOUT_SECONDS", 30) or 0))
    poll_interval = max(0.001, float(getattr(settings, "SES_LIMITER_POLL_INTERVAL_SECONDS", 0.05) or 0.05))
    return {
        "base_rate": base_rate,
        "capacity": max(1.0, burst),
        "reserve": reserve,
        "timeout": timeout,
        "poll_interval": poll_interval,
        "penalty_factor": float(getattr(settings, "SES_LIMITER_THROTTLE_FACTOR", 0.8)),
        "penalty_seconds": max(1.0, float(getattr(settings, "SES_LIMITER_THROTTLE_PENALTY_SECONDS", 60) or 60)),
        "recovery_seconds": max(1.0, float(getattr(settings, "SES_LIMITER_THROTTLE_RECOVERY_SECONDS", 60) or 60)),
        "min_rate": max(0.1, float(getattr(settings, "SES_LIMITER_MIN_RATE_PER_SECOND", 1.0) or 1.0)),
    }


def effective_rate(base_rate, penalty_factor, now_ts, penalty_expires_ts, recovery_seconds):
    """Shared send rate after AIMD state: penalized, then linear recovery."""
    if penalty_factor >= 1.0 or penalty_expires_ts is None or now_ts < penalty_expires_ts:
        return base_rate * penalty_factor
    progress = min(1.0, (now_ts - penalty_expires_ts) / max(1.0, recovery_seconds))
    return base_rate * (penalty_factor + (1.0 - penalty_factor) * progress)


def feedback_penalty(current_effective_rate, base_rate, *, factor=0.8, min_rate=1.0):
    """Penalty factor to store on a Throttling response (multiplicative)."""
    if base_rate <= 0:
        return 1.0
    return max(min_rate / base_rate, (current_effective_rate * factor) / base_rate)


def _discover_account_max_rate():
    """Best-effort SES MaxSendRate for the account/region; None when unknown."""
    if getattr(settings, "TESTING", False):
        return None
    if not getattr(settings, "SES_LIMITER_DISCOVER_SES_QUOTA", True):
        return None
    now_monotonic = time.monotonic()
    if now_monotonic < _account_quota_cache["expires"]:
        return _account_quota_cache["value"]
    value = None
    try:
        from mailing.aws import aws_client  # noqa: PLC0415 - avoid import cycle

        response = aws_client("sesv2", region_name=settings.AWS_SES_REGION).get_account()
        value = float(response.get("SendQuota", {}).get("MaxSendRate") or 0) or None
    except Exception as exc:  # noqa: BLE001 - discovery is best-effort; configured rate stands
        logger.warning("ses_rate_limit_quota_discovery_failed error_type=%s", type(exc).__name__)
    _account_quota_cache.update(expires=now_monotonic + ACCOUNT_QUOTA_CACHE_SECONDS, value=value)
    return value


def resolve_base_rate(config):
    """Configured budget, clamped to the discovered SES MaxSendRate when known."""
    base_rate = config["base_rate"]
    discovered = _discover_account_max_rate()
    if discovered and discovered < base_rate:
        logger.error(
            "ses_rate_limit_clamped_to_ses_quota configured_per_s=%.1f ses_max_send_rate=%.1f",
            base_rate, discovered,
        )
        return discovered
    return base_rate


def _to_ts(value):
    return value.timestamp() if value is not None else None


def acquire_ses_permit(*, cost=1, kind=BULK, timeout=None, key=None):
    """Block until the bucket grants ``cost`` tokens; fail closed on timeout/DB error.

    kind="transactional" may overdraw the bucket by the configured reserve, so
    a saturating campaign delays transactional mail by ~1 s at most. Raises
    :class:`SesRateLimiterTimeout` past the deadline and
    :class:`SesRateLimiterUnavailable` when the limiter's database errors.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown limiter kind: {kind!r}")
    cost = max(1, int(cost))
    config = limiter_config()
    if config["base_rate"] <= 0:
        return
    bucket = key or bucket_key()
    deadline = time.monotonic() + (config["timeout"] if timeout is None else max(0.0, timeout))
    while True:
        try:
            granted, sleep_hint = _try_acquire_once(bucket, cost, kind, config)
        except DatabaseError as exc:
            remaining = deadline - time.monotonic()
            if _is_lock_contention(exc) and remaining > 0:
                logger.debug(
                    "ses_rate_limit_lock_contention bucket=%s kind=%s error_type=%s",
                    bucket, kind, type(exc).__name__,
                )
                time.sleep(min(config["poll_interval"], max(remaining, 0.001)))
                continue
            logger.error(
                "ses_rate_limit_unavailable bucket=%s kind=%s cost=%s error_type=%s",
                bucket, kind, cost, type(exc).__name__,
            )
            raise SesRateLimiterUnavailable(
                f"SES rate limiter database unavailable; not sending (bucket={bucket})"
            ) from exc
        if granted:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            waited = config["timeout"] if timeout is None else timeout
            logger.error(
                "ses_rate_limit_wait_timeout bucket=%s kind=%s cost=%s timeout_s=%.1f",
                bucket, kind, cost, waited,
            )
            raise SesRateLimiterTimeout(
                f"SES rate limiter permit wait timed out; not sending (bucket={bucket})"
            )
        time.sleep(min(max(sleep_hint, 0.001), config["poll_interval"], max(remaining, 0.001)))


def _try_acquire_once(bucket, cost, kind, config):
    """Single atomic refill-and-consume; returns (granted, sleep_hint_seconds)."""
    with transaction.atomic():
        lookup = SesRateLimitBucket.objects.filter(key=bucket)
        if connection.vendor == "postgresql":
            lookup = lookup.select_for_update()
        row = lookup.first()
        if row is None:
            row = _ensure_bucket(bucket, config)
        now = timezone.now()
        base_rate = resolve_base_rate(config)
        now_ts = now.timestamp()
        rate = effective_rate(
            base_rate, row.penalty_factor, now_ts, _to_ts(row.penalty_expires_at), config["recovery_seconds"]
        )
        rate = max(config["min_rate"] if row.penalty_factor < 1.0 else 0.0, rate)
        elapsed = max(0.0, (now - row.last_refill).total_seconds())
        tokens = min(config["capacity"], row.tokens + elapsed * rate)
        recovered = (
            row.penalty_factor < 1.0
            and row.penalty_expires_at is not None
            and now_ts >= _to_ts(row.penalty_expires_at) + config["recovery_seconds"]
        )
        if recovered:
            row.penalty_factor = 1.0
            row.penalty_expires_at = None
        overdraft = config["reserve"] if kind == TRANSACTIONAL else 0.0
        needed = cost - overdraft
        row.tokens = tokens
        row.last_refill = now
        row.base_rate = base_rate
        row.capacity = config["capacity"]
        if tokens >= needed:
            row.tokens = tokens - cost
            row.save(update_fields=[
                "tokens", "last_refill", "base_rate", "capacity",
                "penalty_factor", "penalty_expires_at", "updated_at",
            ])
            return True, 0.0
        row.save(update_fields=[
            "tokens", "last_refill", "base_rate", "capacity",
            "penalty_factor", "penalty_expires_at", "updated_at",
        ])
        deficit = needed - tokens
        return False, deficit / rate if rate > 0 else config["poll_interval"]


def _ensure_bucket(bucket, config):
    now = timezone.now()
    try:
        with transaction.atomic():
            row, _ = SesRateLimitBucket.objects.get_or_create(
                key=bucket,
                defaults={
                    "tokens": config["capacity"],
                    "capacity": config["capacity"],
                    "base_rate": config["base_rate"],
                    "penalty_factor": 1.0,
                    "penalty_expires_at": None,
                    "last_refill": now,
                },
            )
            return row
    except IntegrityError:
        row = SesRateLimitBucket.objects.filter(key=bucket).first()
        if row is None:
            raise
        return row


def report_ses_throttling(*, key=None):
    """AIMD feedback: multiply the shared rate by 0.8 for 60 s. Logged loudly."""
    config = limiter_config()
    if config["base_rate"] <= 0:
        logger.debug("ses_rate_limit_feedback_ignored_limiter_disabled")
        return
    bucket = key or bucket_key()
    try:
        with transaction.atomic():
            lookup = SesRateLimitBucket.objects.filter(key=bucket)
            if connection.vendor == "postgresql":
                lookup = lookup.select_for_update()
            row = lookup.first()
            if row is None:
                row = _ensure_bucket(bucket, config)
            now = timezone.now()
            base_rate = resolve_base_rate(config)
            current = effective_rate(
                base_rate, row.penalty_factor, now.timestamp(),
                _to_ts(row.penalty_expires_at), config["recovery_seconds"],
            )
            row.penalty_factor = feedback_penalty(
                current, base_rate, factor=config["penalty_factor"], min_rate=config["min_rate"]
            )
            row.penalty_expires_at = now + timedelta(seconds=config["penalty_seconds"])
            row.base_rate = base_rate
            row.save(update_fields=["penalty_factor", "penalty_expires_at", "base_rate", "updated_at"])
            penalized = base_rate * row.penalty_factor
    except DatabaseError as exc:
        logger.error(
            "ses_rate_limit_feedback_unavailable bucket=%s error_type=%s",
            bucket, type(exc).__name__,
        )
        raise SesRateLimiterUnavailable(
            f"SES rate limiter database unavailable; not sending (bucket={bucket})"
        ) from exc
    logger.error(
        "ses_rate_limit_throttling_feedback bucket=%s shared_rate_per_s=%.1f base_rate_per_s=%.1f "
        "penalty_factor=%.2f penalty_s=%.0f",
        bucket, penalized, base_rate, row.penalty_factor, config["penalty_seconds"],
    )
