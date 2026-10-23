"""Ownership of the finished task-result prune sweep (t1143u relay-aux).

The scheduler loop is the single owner of ``prune_db_task_results``: no
Coolify scheduled task and no retired AWS rule may prune the same table.
These tests pin the daily gate, the exact retention arguments, the
never-raise contract that keeps a hygiene failure out of the 15-second loop,
and the wiring from ``run_relay_scheduler``.
"""

from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

import jobs.services as services
from jobs.services import (
    TASK_RESULT_FAILED_RETENTION_DAYS,
    TASK_RESULT_RETENTION_DAYS,
    prune_finished_task_results,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def reset_prune_gate():
    """Module-level day gate must not leak between tests in one process."""
    services._TASK_RESULTS_PRUNE_LAST_RUN_DATE = None
    yield
    services._TASK_RESULTS_PRUNE_LAST_RUN_DATE = None


def test_prune_runs_once_per_utc_day_with_retention_args():
    with patch("jobs.services.call_command") as call:
        assert prune_finished_task_results() == 1
        assert prune_finished_task_results() == 0
    assert call.call_count == 1
    _, kwargs = call.call_args
    assert kwargs["queue_name"] == "*"
    assert kwargs["min_age_days"] == TASK_RESULT_RETENTION_DAYS == 14
    assert kwargs["failed_min_age_days"] == TASK_RESULT_FAILED_RETENTION_DAYS == 14


def test_prune_runs_again_on_the_next_utc_day():
    today = timezone.now().date()
    with patch("jobs.services.call_command") as call:
        assert prune_finished_task_results(today=today) == 1
        assert prune_finished_task_results(today=today) == 0
        assert prune_finished_task_results(today=today + timedelta(days=1)) == 1
    assert call.call_count == 2


def test_prune_failure_never_raises_and_stays_gated_that_day():
    with patch("jobs.services.call_command", side_effect=RuntimeError("db down")) as call:
        assert prune_finished_task_results() == 0
        assert prune_finished_task_results() == 0
    # One attempt per day even when it fails: a broken sweep must not hammer
    # the database every 15 seconds from inside the scheduler loop.
    assert call.call_count == 1


def test_scheduler_pass_invokes_the_prune_sweep():
    with patch(
        "jobs.management.commands.run_relay_scheduler.dispatch_due_campaigns", return_value=[]
    ), patch(
        "jobs.management.commands.run_relay_scheduler.run_due_schedules", return_value=[]
    ), patch(
        "jobs.management.commands.run_relay_scheduler.fail_expired_leases", return_value=0
    ), patch(
        "jobs.management.commands.run_relay_scheduler.recover_unenqueued_jobs", return_value=0
    ), patch(
        "jobs.management.commands.run_relay_scheduler.prune_finished_task_results", return_value=1
    ) as prune:
        call_command("run_relay_scheduler", once=True, verbosity=0)
    assert prune.call_count == 1


def test_prune_only_touches_finished_results_past_retention():
    """End-to-end against the real upstream command on a scratch database."""
    old = timezone.now() - timedelta(days=TASK_RESULT_RETENTION_DAYS + 1)
    base = {
        "task_path": "jobs.tasks.echo",
        "backend_name": "default",
        "args_kwargs": {"args": [], "kwargs": {}},
        "run_after": old,
        "exception_class_path": "",
        "traceback": "",
    }
    stale = DBTaskResult.objects.create(queue_name="default", status="SUCCESSFUL", **base)
    DBTaskResult.objects.filter(pk=stale.pk).update(finished_at=old, started_at=old)
    fresh = DBTaskResult.objects.create(queue_name="provider-events", status="SUCCESSFUL", **base)

    assert prune_finished_task_results() == 1
    remaining = set(DBTaskResult.objects.values_list("pk", flat=True))
    assert stale.pk not in remaining
    assert fresh.pk in remaining


def test_explicit_today_boundary_uses_date_not_datetime():
    with patch("jobs.services.call_command") as call:
        assert prune_finished_task_results(today=date(2026, 10, 23)) == 1
        assert prune_finished_task_results(today=date(2026, 10, 24)) == 1
    assert call.call_count == 2
