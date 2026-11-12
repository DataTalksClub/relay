"""A brief Postgres outage must not stop the scheduler loop.

The scheduler is a bare ``while True`` loop whose only liveness signal is
its heartbeat row: if one pass raises, the loop exits, the container looks
healthy, and nothing fires. A Coolify network reattach or a Postgres IP
churn costs seconds, so one pass absorbs it with retries instead of dying.
"""

from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import OperationalError

from jobs.models import SchedulerHeartbeat
from mailing import db_retry

# transaction=True: the retry path closes connections, which a wrapping
# test transaction would not survive on PostgreSQL.
pytestmark = pytest.mark.django_db(transaction=True)


def _patched_pass(**overrides):
    defaults = {
        "jobs.management.commands.run_relay_scheduler.fail_expired_leases": 0,
        "jobs.management.commands.run_relay_scheduler.recover_unenqueued_jobs": 0,
        "jobs.management.commands.run_relay_scheduler.run_due_schedules": [],
        "jobs.management.commands.run_relay_scheduler.dispatch_due_campaigns": [],
        "jobs.management.commands.run_relay_scheduler.prune_finished_task_results": False,
        "jobs.management.commands.run_relay_scheduler.alert_stuck_transactional_messages": None,
        "jobs.management.commands.run_relay_scheduler.check_transactional_health": None,
        "jobs.management.commands.run_relay_scheduler.check_ses_delivery_gap": None,
    }
    defaults.update(overrides)
    return [patch(target, return_value=value) for target, value in defaults.items()]


def _run_patched(patches, **kwargs):
    for entered in patches:
        entered.start()
    try:
        call_command("run_relay_scheduler", verbosity=0, **kwargs)
    finally:
        for entered in patches:
            entered.stop()


def test_a_transient_error_retries_within_one_pass(monkeypatch):
    monkeypatch.setattr(db_retry, "sleep", lambda seconds: None)
    patches = _patched_pass()
    fail = patch(
        "jobs.management.commands.run_relay_scheduler.fail_expired_leases",
        side_effect=[OperationalError("deadlock detected"), 0],
    )
    patches[0] = fail
    _run_patched(patches, once=True)

    assert SchedulerHeartbeat.objects.count() == 1


class _StopLoop(Exception):
    """Sentinel to break the continuous loop after the assertions are set up."""


def test_continuous_mode_survives_an_outage_longer_than_one_pass(monkeypatch):
    """An exhausted pass must wait out the outage, not exit the loop.

    Three failed attempts (sleeps 1s + 2s) exceed one pass; the loop then
    waits 8s, continues, writes a heartbeat on the next healthy pass, and
    only stops here because the sentinel breaks the test loop.
    """
    sleeps = []
    monkeypatch.setattr(db_retry, "sleep", sleeps.append)
    outage = OperationalError('could not translate host name "db"')
    failing = patch(
        "jobs.management.commands.run_relay_scheduler.fail_expired_leases",
        side_effect=[outage, outage, outage, 0, _StopLoop()],
    )
    patches = _patched_pass()
    patches[0] = failing

    with pytest.raises(_StopLoop):
        _run_patched(patches, interval=15.0)

    assert SchedulerHeartbeat.objects.count() == 1
    # Two jittered exponential retry steps, then the outage pause and the
    # scheduler's own idle interval (both un jittered).
    assert 1.0 <= sleeps[0] <= 1.25
    assert 2.0 <= sleeps[1] <= 2.5
    assert sleeps[2:] == [8.0, 15.0]


def test_a_persistent_dns_miss_fails_loudly_with_once(monkeypatch):
    sleeps = []
    monkeypatch.setattr(db_retry, "sleep", sleeps.append)
    patches = _patched_pass()
    always_failing = patch(
        "jobs.management.commands.run_relay_scheduler.fail_expired_leases",
        side_effect=OperationalError('could not translate host name "db"'),
    )
    patches[0] = always_failing

    with pytest.raises(OperationalError, match="could not translate host name"):
        _run_patched(patches, once=True)
    assert SchedulerHeartbeat.objects.count() == 0
    # Two jittered exponential retry steps, then once-mode re-raises.
    assert 1.0 <= sleeps[0] <= 1.25
    assert 2.0 <= sleeps[1] <= 2.5


def test_a_permanent_error_raises_without_retry(monkeypatch):
    sleeps = []
    monkeypatch.setattr(db_retry, "sleep", sleeps.append)
    bad = patch(
        "jobs.management.commands.run_relay_scheduler.fail_expired_leases",
        side_effect=OperationalError('relation "jobs_schedulerheartbeat" does not exist'),
    )
    patches = _patched_pass()
    patches[0] = bad

    with pytest.raises(OperationalError, match="does not exist"):
        _run_patched(patches, once=True)
    assert sleeps == []
