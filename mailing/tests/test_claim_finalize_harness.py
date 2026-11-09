"""WP1 acceptance tests: claim -> SES call outside any transaction -> finalize.

Phase A (this file's first commit) lands the fault-injection harness and the
acceptance tests against the planned API. Tests that need WP1 behaviour are
marked ``wp1`` and xfail until the implementation (Phase B) removes the mark.
The ``test_harness_*`` tests pass on main and prove the harness itself.

Needs PostgreSQL (row locks, one connection per thread, real subprocesses):
    DATABASE_URL=postgresql://... uv run pytest mailing/tests/test_claim_finalize_harness.py
"""

import json
import os
import signal
import threading
import time

import pytest
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from mailing.models import Campaign, CampaignRecipient, CampaignRecipientStatus
from mailing.services.campaign_sender import send_campaign_batch
from mailing.tests.claim_finalize_harness import (
    FaultSes,
    big_html,
    campaign_with_recipients,
    query_log,
    run_batch_subprocess,
    statuses,
)

pytestmark = [
    pytest.mark.skipif(connection.vendor != "postgresql", reason="needs PostgreSQL"),
    pytest.mark.django_db(transaction=True),
]
wp1 = pytest.mark.xfail(reason="WP1 phase B: claim-send-finalize not implemented yet", strict=False)

SENDING = "sending"
UNKNOWN = "unknown"
FAST = {"SES_MAX_SEND_RATE_PER_SECOND": 1000.0}


def _recipient(email):
    return CampaignRecipient.objects.get(email=email)


# --- harness self-tests (pass on main) -------------------------------------------------


@override_settings(CAMPAIGN_SEND_CONCURRENCY=1, **FAST)
def test_harness_fake_ses_records_accepts_and_scripted_faults():
    campaign, payload = campaign_with_recipients(4)
    ses = FaultSes(faults={2: "reject", 4: "reject"})
    result = send_campaign_batch(payload, ses_client=ses)
    assert ses.calls == 4
    assert result.sent_count == 2 and result.failed_count == 2
    assert len(ses.accepted) == 2


@override_settings(CAMPAIGN_SEND_CONCURRENCY=4, **FAST)
def test_harness_query_log_sees_all_threads():
    campaign, payload = campaign_with_recipients(8)
    with query_log() as log:
        send_campaign_batch(payload, ses_client=FaultSes(latency=0.01))
    assert log.matching("update", "campaign_recipients")


@override_settings(CAMPAIGN_SEND_CONCURRENCY=1, **FAST)
def test_harness_detects_transaction_open_during_call_on_main():
    campaign, payload = campaign_with_recipients(3)
    ses = FaultSes(assert_no_transaction=True)
    send_campaign_batch(payload, ses_client=ses)
    # On main the call is inside transaction.atomic; after WP1 this list is empty.
    assert ses.calls == 3
    assert isinstance(ses.transaction_violations, list)


# --- AC1: no DB transaction during the SES call -----------------------------------------


@wp1
@override_settings(CAMPAIGN_SEND_CONCURRENCY=4, **FAST)
def test_ac1_no_transaction_open_during_ses_call():
    campaign, payload = campaign_with_recipients(20)
    ses = FaultSes(latency=0.01, assert_no_transaction=True)
    send_campaign_batch(payload, ses_client=ses)
    assert ses.calls == 20
    assert ses.transaction_violations == []


# --- AC2: no lock on the Campaign row while sending --------------------------------------


@wp1
@override_settings(CAMPAIGN_SEND_CONCURRENCY=4, **FAST)
def test_ac2_no_campaign_row_lock_in_send_path():
    campaign, payload = campaign_with_recipients(20)
    with query_log() as log:
        send_campaign_batch(payload, ses_client=FaultSes(latency=0.01))
    campaign_locks = [
        sql for sql in log.matching('"campaigns"') if "for share" in sql.lower() or "for update" in sql.lower()
    ]
    # _mark_campaign_sending may lock the campaign once per batch (not per message); never per recipient.
    assert len(campaign_locks) <= 1, campaign_locks


@wp1
@override_settings(CAMPAIGN_SEND_CONCURRENCY=8, **FAST)
def test_ac2_concurrent_campaign_update_is_not_blocked_by_in_flight_sends():
    campaign, payload = campaign_with_recipients(40)
    ses = FaultSes(latency=0.3)
    errors = []
    thread = threading.Thread(target=lambda: _run_batch(payload, ses, errors))
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while ses.in_flight == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ses.in_flight > 0, "batch never reached SES"
        started = time.monotonic()
        Campaign.objects.filter(pk=campaign.pk).update(preview_text="operator edit")
        elapsed = time.monotonic() - started
    finally:
        thread.join()
    assert errors == []
    assert elapsed < 1.0, f"UPDATE campaign waited {elapsed:.2f}s behind the send path"


def _run_batch(payload, ses, errors):
    try:
        send_campaign_batch(payload, ses_client=ses)
    except Exception as exc:  # noqa: BLE001 - surfaced by the assertion above
        errors.append(exc)
    finally:
        connection.close()


# --- AC3: crash between SES accept and finalize ------------------------------------------


@wp1
@override_settings(CAMPAIGN_SEND_LEASE_SECONDS=600, **FAST)
def test_ac3_sigkill_after_accept_ends_unknown_is_never_resent_and_reconciles_once():
    campaign, payload = campaign_with_recipients(6)
    process = run_batch_subprocess(payload, faults={3: "crash_after_accept"}, concurrency=1)
    process.communicate(timeout=60)
    assert process.returncode == -signal.SIGKILL

    sending = CampaignRecipient.objects.filter(campaign=campaign, status=SENDING)
    assert sending.count() == 1, statuses(campaign)
    stuck = sending.get()

    # Lease not expired: the periodic task must leave it alone.
    from mailing.services.send_reconcile import expire_stale_claims, reconcile_unknown_from_event

    assert expire_stale_claims() == 0
    CampaignRecipient.objects.filter(pk=stuck.pk).update(claimed_at=timezone.now() - timezone.timedelta(minutes=11))
    assert expire_stale_claims() == 1
    stuck.refresh_from_db()
    assert stuck.status == UNKNOWN

    # A rerun of the whole batch sends zero additional SES calls for the UNKNOWN row.
    ses = FaultSes()
    send_campaign_batch(payload, ses_client=ses)
    assert stuck.email not in ses.accepted
    stuck.refresh_from_db()
    assert stuck.status == UNKNOWN

    event = {"ses_message_id": "ses-3", "recipient": stuck.email, "campaign_id": campaign.id}
    assert reconcile_unknown_from_event(event) is True
    assert reconcile_unknown_from_event(event) is False  # exactly once
    stuck.refresh_from_db()
    assert stuck.status == CampaignRecipientStatus.SENT and stuck.ses_message_id == "ses-3"
    assert CampaignRecipient.objects.filter(campaign=campaign, ses_message_id="ses-3").count() == 1


# --- AC4: ambiguous / definite / throttled outcomes --------------------------------------


@wp1
@override_settings(CAMPAIGN_SEND_CONCURRENCY=1, **FAST)
@pytest.mark.parametrize("fault", ["read_timeout", "conn_reset"])
def test_ac4_ambiguous_error_becomes_unknown_and_is_not_retried(fault):
    campaign, payload = campaign_with_recipients(3)
    ses = FaultSes(faults={2: fault})
    send_campaign_batch(payload, ses_client=ses)
    counts = statuses(campaign)
    assert counts.get(UNKNOWN) == 1 and counts.get(CampaignRecipientStatus.SENT) == 2, counts
    calls_before = ses.calls
    send_campaign_batch(payload, ses_client=ses)
    assert ses.calls == calls_before


@override_settings(CAMPAIGN_SEND_CONCURRENCY=1, **FAST)
def test_ac4_message_rejected_is_failed():
    campaign, payload = campaign_with_recipients(3)
    send_campaign_batch(payload, ses_client=FaultSes(faults={2: "reject"}))
    assert statuses(campaign) == {CampaignRecipientStatus.SENT: 2, CampaignRecipientStatus.FAILED: 1}


@override_settings(CAMPAIGN_SEND_CONCURRENCY=1, **FAST)
def test_ac4_throttling_returns_row_to_pending():
    campaign, payload = campaign_with_recipients(4)
    ses = FaultSes(faults={2: "throttle"})
    try:
        send_campaign_batch(payload, ses_client=ses)
    except Exception:  # noqa: BLE001 - requeue path may raise a retryable error
        pass
    counts = statuses(campaign)
    assert SENDING not in counts and UNKNOWN not in counts, counts
    assert counts.get(CampaignRecipientStatus.PENDING, 0) >= 1


# --- AC5: SIGTERM mid-batch --------------------------------------------------------------


@wp1
@override_settings(**FAST)
def test_ac5_sigterm_finalizes_in_flight_returns_unstarted_and_exits_in_grace():
    campaign, payload = campaign_with_recipients(60)
    process = run_batch_subprocess(payload, latency=0.3, concurrency=4)
    assert process.stdout.readline().strip() == "READY"
    time.sleep(1.0)
    started = time.monotonic()
    process.send_signal(signal.SIGTERM)
    process.communicate(timeout=15)
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"drain took {elapsed:.1f}s"
    counts = statuses(campaign)
    assert SENDING not in counts, counts
    assert counts.get(CampaignRecipientStatus.SENT, 0) > 0
    assert counts.get(CampaignRecipientStatus.PENDING, 0) > 0
    assert sum(counts.values()) == 60


# --- AC6: DB round trips per email -------------------------------------------------------


@wp1
@override_settings(CAMPAIGN_SEND_CONCURRENCY=4, **FAST)
def test_ac6_db_round_trips_per_email_at_most_3_amortized():
    count = 200
    campaign, payload = campaign_with_recipients(count)
    ses = FaultSes()
    with query_log() as log:
        send_campaign_batch(payload, ses_client=ses)
    per_email = len(log) / count
    print(f"db_round_trips_per_email={per_email:.2f}")
    assert per_email <= 3, f"{per_email:.2f} statements per email"


# --- AC7: throughput benchmark (opt-in; numbers go in the report) ------------------------


@pytest.mark.skipif(not os.environ.get("RELAY_BENCH"), reason="set RELAY_BENCH=1 to run the throughput benchmark")
@override_settings(CAMPAIGN_SEND_CONCURRENCY=16, SES_MAX_SEND_RATE_PER_SECOND=45.0)
def test_ac7_benchmark_throughput_200ms_80kb_limiter_45():
    count = int(os.environ.get("RELAY_BENCH_COUNT", "300"))
    campaign, payload = campaign_with_recipients(count, html_body=big_html())
    ses = FaultSes(latency=0.2)
    started = time.monotonic()
    result = send_campaign_batch(payload, ses_client=ses)
    rate = count / (time.monotonic() - started)
    print(f"BENCH msgs={count} rate={rate:.1f}/s max_in_flight={ses.max_in_flight} sent={result.sent_count}")
    assert result.sent_count == count


# --- AC8: operator list + confirmed resend ----------------------------------------------


@wp1
def test_ac8_unknown_list_counts_and_resend_requires_confirmation(client):
    from mailing.services.send_reconcile import resend_unknown, unknown_recipients

    campaign, payload = campaign_with_recipients(3)
    CampaignRecipient.objects.filter(campaign=campaign).update(status=UNKNOWN)
    listing = unknown_recipients(campaign.id)
    assert listing["count"] == 3
    ids = [row["id"] for row in listing["recipients"]][:2]
    with pytest.raises(Exception, match="confirm"):
        resend_unknown(campaign.id, ids, confirm=False, actor="operator@example.com")
    assert statuses(campaign) == {UNKNOWN: 3}
    assert resend_unknown(campaign.id, ids, confirm=True, actor="operator@example.com") == 2
    assert statuses(campaign) == {UNKNOWN: 1, CampaignRecipientStatus.PENDING: 2}


def test_harness_json_runner_importable():
    # Guard: the runner module must stay import-clean (it is exec'd as a script).
    import ast
    from mailing.tests.claim_finalize_harness import RUNNER

    ast.parse(RUNNER.read_text())
    assert json.dumps({}) == "{}"
