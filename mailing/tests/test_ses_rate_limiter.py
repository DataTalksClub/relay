"""WP2 acceptance tests: global account-wide SES token bucket.

AC1: concurrent senders never exceed the configured rate over any 1 s window.
AC2: no SES client send call outside mailing/ses.py (grep guard; behavioral
     half lives in test_ses.py).
AC3: transactional sends get their reserve under campaign saturation.
AC4: throttling feedback lowers the shared rate and recovers (fake clock).
AC5: limiter DB error fails closed (no SES call, loud error); waits time out.
AC6: limiter overhead < 2 ms per permit at 45/s (benchmark).
"""

import logging
import re
import threading
import time
from pathlib import Path

import pytest
from django.conf import settings
from django.db import connection
from django.db.utils import OperationalError
from django.test import override_settings

from mailing.models import SesRateLimitBucket
from mailing.ses import send_email
from mailing.ses_rate_limiter import (
    TRANSACTIONAL,
    SesRateLimiterTimeout,
    SesRateLimiterUnavailable,
    acquire_ses_permit,
    bucket_key,
    effective_rate,
    feedback_penalty,
    report_ses_throttling,
)


def _close_thread_connection():
    try:
        connection.close()
    except Exception:  # noqa: BLE001 - test hygiene only
        pass


# AC1 -----------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@override_settings(
    SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND=10.0,
    SES_LIMITER_BURST_MESSAGES=2.0,
    SES_TRANSACTIONAL_RESERVE_PER_SECOND=1.0,
)
def test_concurrent_senders_share_one_global_budget():
    """Two or more senders (threads, separate DB connections) stay within
    rate + burst over every 1 s window."""
    grant_times = []
    lock = threading.Lock()

    def worker(count):
        try:
            for _ in range(count):
                acquire_ses_permit(cost=1, kind="bulk")
                with lock:
                    grant_times.append(time.monotonic())
        finally:
            _close_thread_connection()

    threads = [threading.Thread(target=worker, args=(6,)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    assert len(grant_times) == 24

    ordered = sorted(grant_times)
    for stamp in ordered:
        in_window = sum(1 for other in ordered if stamp <= other < stamp + 1.0)
        assert in_window <= 10 + 2, f"{in_window} grants within 1 s at {stamp}"
    # The bucket actually limited: 24 permits at 10/s take ~2.2 s, not ~0 s.
    assert ordered[-1] - ordered[0] >= 1.5


# AC2 -----------------------------------------------------------------------


def test_no_direct_ses_send_calls_outside_ses_module():
    """Every SES call site goes through the limiter in mailing/ses.py."""
    root = Path(settings.BASE_DIR)
    offenders = []
    for path in root.rglob("*.py"):
        if ".venv" in path.parts or ".git" in path.parts:
            continue
        if path.name.startswith(".") or ".aplexer-tasks" in path.parts:
            continue
        if path == root / "mailing" / "ses.py":
            continue
        if "tests" in path.parts or "test_" in path.name or path.name.endswith("_test.py"):
            continue
        if path.name == "smoke_test_sandbox.py":
            continue  # excluded from WP2 scope by the brief
        if re.search(r"\.(send_email|send_raw_email|send_bulk_email)\s*\(", path.read_text()):
            offenders.append(str(path.relative_to(root)))
    assert offenders == []


# AC3 -----------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@override_settings(
    SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND=4.0,
    SES_LIMITER_BURST_MESSAGES=0.0,
    SES_TRANSACTIONAL_RESERVE_PER_SECOND=2.0,
)
def test_transactional_reserve_beats_campaign_saturation():
    """While bulk sends saturate the bucket, a transactional permit waits < 1 s."""
    stop = threading.Event()

    def bulk_saturate():
        try:
            while not stop.is_set():
                acquire_ses_permit(cost=1, kind="bulk")
        finally:
            _close_thread_connection()

    saturator = threading.Thread(target=bulk_saturate)
    saturator.start()
    try:
        time.sleep(1.0)  # let bulk drain the bucket into steady state
        started = time.monotonic()
        acquire_ses_permit(cost=1, kind=TRANSACTIONAL, timeout=10)
        waited = time.monotonic() - started
    finally:
        stop.set()
        saturator.join(timeout=30)
    assert not saturator.is_alive()
    assert waited < 1.0, f"transactional permit waited {waited:.2f} s under saturation"


# AC4 -----------------------------------------------------------------------


def test_throttling_feedback_lowers_rate_and_recovers_with_fake_clock():
    base, recovery = 45.0, 60.0
    now = 1_000_000.0

    assert effective_rate(base, 1.0, now, None, recovery) == pytest.approx(45.0)

    factor = feedback_penalty(45.0, base)
    expires = now + 60.0
    assert factor == pytest.approx(0.8)
    assert effective_rate(base, factor, now + 1, expires, recovery) == pytest.approx(36.0)
    assert effective_rate(base, factor, expires - 1, expires, recovery) == pytest.approx(36.0)
    # Linear recovery after the penalty window.
    assert effective_rate(base, factor, expires + 30, expires, recovery) == pytest.approx(40.5)
    assert effective_rate(base, factor, expires + 60, expires, recovery) == pytest.approx(45.0)
    assert effective_rate(base, factor, expires + 600, expires, recovery) == pytest.approx(45.0)

    # A second Throttling response while penalized multiplies again.
    factor2 = feedback_penalty(36.0, base)
    assert factor2 == pytest.approx(0.64)
    assert base * factor2 == pytest.approx(28.8)


@pytest.mark.django_db(transaction=True)
@override_settings(
    SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND=45.0,
    SES_LIMITER_BURST_MESSAGES=9.0,
    SES_LIMITER_THROTTLE_PENALTY_SECONDS=60.0,
)
def test_report_throttling_persists_penalty_and_logs_loudly(caplog):
    acquire_ses_permit(cost=1, kind="bulk")
    with caplog.at_level(logging.ERROR, logger="mailing.ses_rate_limiter"):
        report_ses_throttling()
    row = SesRateLimitBucket.objects.get(key=bucket_key())
    assert row.penalty_factor == pytest.approx(0.8)
    assert row.penalty_expires_at is not None
    assert any(
        "ses_rate_limit_throttling_feedback" in record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR
    )


# AC5 -----------------------------------------------------------------------


class _RecordingSesClient:
    def __init__(self):
        self.calls = []

    def send_email(self, **params):
        self.calls.append(params)
        return {"MessageId": "never"}

    def send_raw_email(self, **params):
        self.calls.append(params)
        return {"MessageId": "never"}


@pytest.mark.django_db(transaction=True)
@override_settings(SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND=45.0)
def test_limiter_db_error_fails_closed_without_ses_call(monkeypatch, caplog):
    ses = _RecordingSesClient()

    def boom(*_args, **_kwargs):
        raise OperationalError("connection failed")

    monkeypatch.setattr(SesRateLimitBucket.objects, "filter", boom)
    with caplog.at_level(logging.ERROR, logger="mailing.ses_rate_limiter"):
        with pytest.raises(SesRateLimiterUnavailable):
            send_email(
                ses_client=ses,
                source="newsletter@example.com",
                to_email="person@example.com",
                subject="Welcome",
                html_body="<p>Hello</p>",
            )
    assert ses.calls == []
    assert any(
        "ses_rate_limit_unavailable" in record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR
    )


@pytest.mark.django_db(transaction=True)
@override_settings(
    SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND=2.0,
    SES_LIMITER_BURST_MESSAGES=0.0,
    SES_TRANSACTIONAL_RESERVE_PER_SECOND=0.5,
)
def test_permit_wait_timeout_is_retryable_and_loud(caplog):
    acquire_ses_permit(cost=1, kind="bulk")  # drains the single-token bucket
    with caplog.at_level(logging.ERROR, logger="mailing.ses_rate_limiter"):
        with pytest.raises(SesRateLimiterTimeout) as exc_info:
            acquire_ses_permit(cost=1, kind="bulk", timeout=0.3)
    assert isinstance(exc_info.value, SesRateLimiterUnavailable)
    assert any(
        "ses_rate_limit_wait_timeout" in record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR
    )


# AC6 -----------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@override_settings(
    SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND=1000.0,
    SES_LIMITER_BURST_MESSAGES=1000.0,
    SES_TRANSACTIONAL_RESERVE_PER_SECOND=5.0,
)
def test_limiter_overhead_under_2ms_per_permit():
    acquire_ses_permit(cost=1, kind="bulk")  # create the warm bucket row
    count = 100
    started = time.perf_counter()
    for _ in range(count):
        acquire_ses_permit(cost=1, kind="bulk")
    mean_ms = (time.perf_counter() - started) / count * 1000
    print(f"\nlimiter overhead: {mean_ms:.3f} ms per permit (budget 2 ms)")
    assert mean_ms < 2.0, f"limiter overhead {mean_ms:.3f} ms per permit"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("target_rate", [40.0, 240.0])
def test_single_config_scales_aggregate_rate_across_workers(target_rate, settings):
    """Changing the ONE config number (SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND)
    scales aggregate throughput across concurrent workers to ~target_rate
    (e.g. 40/s -> 240/s) with mocked SES and zero real sends."""
    settings.SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND = target_rate
    settings.SES_LIMITER_BURST_MESSAGES = 0.0  # default: rate / 5
    settings.SES_TRANSACTIONAL_RESERVE_PER_SECOND = 10.0

    class MockSes:
        def __init__(self):
            self.lock = threading.Lock()
            self.timestamps = []

        def send_email(self, **_kwargs):
            with self.lock:
                self.timestamps.append(time.monotonic())
            return {"MessageId": f"mock-{len(self.timestamps)}"}

    ses = MockSes()
    # Pre-warm and drain the initial burst so steady-state rate is measured cleanly.
    acquire_ses_permit(cost=1, kind="bulk")
    SesRateLimitBucket.objects.filter(key=bucket_key()).update(tokens=0.0)

    duration_s = 1.2
    stop_at = time.monotonic() + duration_s
    worker_count = 6

    def worker(idx):
        try:
            n = 0
            while time.monotonic() < stop_at:
                send_email(
                    ses_client=ses,
                    source="sender@example.com",
                    to_email=f"w{idx}-{n}@example.com",
                    subject="Rate test",
                    html_body="<p>hi</p>",
                    text_body="hi",
                    kind="bulk",
                )
                n += 1
        finally:
            _close_thread_connection()

    started = time.monotonic()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(worker_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    elapsed = time.monotonic() - started
    assert not any(t.is_alive() for t in threads)

    ordered = sorted(ses.timestamps)
    achieved_rate = len(ordered) / elapsed
    burst = target_rate / 5.0
    # Aggregate rate matches the single configured target_rate (neither under-fed nor uncapped).
    assert target_rate * 0.75 <= achieved_rate <= target_rate * 1.25, (
        f"target={target_rate}/s achieved={achieved_rate:.1f}/s ({len(ordered)} sends in {elapsed:.2f}s)"
    )
    for stamp in ordered:
        in_window = sum(1 for other in ordered if stamp <= other < stamp + 1.0)
        assert in_window <= target_rate + burst + 2, f"{in_window} sends in 1s window at target={target_rate}"


@pytest.mark.django_db(transaction=True)
@override_settings(
    SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND=240.0,
    SES_LIMITER_BURST_MESSAGES=0.0,
    SES_TRANSACTIONAL_RESERVE_PER_SECOND=10.0,
)
def test_transactional_sends_promptly_while_campaign_runs_at_240():
    """While bulk campaign workers saturate at 240/s, transactional mail still
    sends promptly using the 10/s reserve and is not starved."""
    class MockSes:
        def __init__(self):
            self.lock = threading.Lock()
            self.bulk_count = 0
            self.tx_count = 0

        def send_email(self, **kwargs):
            to_addr = kwargs["Destination"]["ToAddresses"][0]
            with self.lock:
                if to_addr.startswith("tx-"):
                    self.tx_count += 1
                else:
                    self.bulk_count += 1
            return {"MessageId": "mock-id"}

    ses = MockSes()
    stop = threading.Event()

    def bulk_worker():
        try:
            while not stop.is_set():
                send_email(
                    ses_client=ses,
                    source="sender@example.com",
                    to_email="bulk@example.com",
                    subject="Campaign",
                    html_body="<p>bulk</p>",
                    text_body="bulk",
                    kind="bulk",
                )
        finally:
            _close_thread_connection()

    workers = [threading.Thread(target=bulk_worker) for _ in range(4)]
    for w in workers:
        w.start()
    try:
        time.sleep(0.4)
        tx_latencies = []
        for i in range(5):
            t0 = time.monotonic()
            send_email(
                ses_client=ses,
                source="sender@example.com",
                to_email=f"tx-{i}@example.com",
                subject="Reset password",
                html_body="<p>tx</p>",
                text_body="tx",
                kind=TRANSACTIONAL,
            )
            tx_latencies.append(time.monotonic() - t0)
    finally:
        stop.set()
        for w in workers:
            w.join(timeout=30)
    assert not any(w.is_alive() for w in workers)
    assert ses.bulk_count > 20
    assert ses.tx_count == 5
    assert max(tx_latencies) < 0.5, f"transactional send delayed: max={max(tx_latencies):.3f}s"
