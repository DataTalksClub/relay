"""The scheduler's own liveness row.

The scheduler is a bare ``while True`` loop with no health endpoint, so a loop
that stops looping is invisible: the container stays up, the worker stays up,
and nothing fires. These tests cover the signal that makes the difference
observable, and the two places it surfaces.
"""

import os
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from jobs.models import SchedulerHeartbeat
from jobs.services import heartbeat_state, record_scheduler_heartbeat

pytestmark = pytest.mark.django_db


@pytest.fixture
def operator(client):
    client.force_login(get_user_model().objects.create_user(username="ops", is_staff=True))
    return client


@pytest.fixture
def silent_scheduler():
    """Freeze the scheduler's other sweeps so a pass does one thing only."""
    with patch(
        "jobs.management.commands.run_relay_scheduler.dispatch_due_campaigns", return_value=[]
    ), patch(
        "jobs.management.commands.run_relay_scheduler.run_due_schedules", return_value=[]
    ), patch(
        "jobs.management.commands.run_relay_scheduler.fail_expired_leases", return_value=0
    ), patch(
        "jobs.management.commands.run_relay_scheduler.recover_unenqueued_jobs", return_value=0
    ):
        yield


def test_a_pass_writes_a_heartbeat_even_when_nothing_was_due(silent_scheduler):
    """"Nothing fired" and "not running" must not look the same.

    An empty pass is the normal state for most of the day, so the write has to
    happen unconditionally rather than only when something fired.
    """
    call_command("run_relay_scheduler", once=True, verbosity=0)

    heartbeat = SchedulerHeartbeat.objects.get()
    assert heartbeat.pid == os.getpid()
    assert heartbeat.ticked_at <= timezone.now()
    assert heartbeat.schedules_fired == 0


def test_two_passes_advance_the_tick(silent_scheduler):
    call_command("run_relay_scheduler", once=True, verbosity=0)
    first = SchedulerHeartbeat.objects.get().ticked_at

    call_command("run_relay_scheduler", once=True, verbosity=0)

    assert SchedulerHeartbeat.objects.get().ticked_at > first


def test_the_tick_carries_what_the_pass_did():
    with patch(
        "jobs.management.commands.run_relay_scheduler.dispatch_due_campaigns",
        return_value=[object(), object()],
    ), patch(
        "jobs.management.commands.run_relay_scheduler.run_due_schedules", return_value=[object()]
    ), patch(
        "jobs.management.commands.run_relay_scheduler.fail_expired_leases", return_value=2
    ), patch(
        "jobs.management.commands.run_relay_scheduler.recover_unenqueued_jobs", return_value=3
    ):
        call_command("run_relay_scheduler", once=True, verbosity=0)
        call_command("run_relay_scheduler", once=True, verbosity=0)

    heartbeat = SchedulerHeartbeat.objects.get()
    assert heartbeat.schedules_fired == 2
    assert heartbeat.expired_leases == 4
    assert heartbeat.recovered_jobs == 6
    assert heartbeat.campaigns_dispatched == 4


def test_counters_belong_to_a_process_not_to_the_table():
    """A restarted container must not inherit the previous run's totals."""
    record_scheduler_heartbeat(schedules_fired=7)
    assert SchedulerHeartbeat.objects.get().schedules_fired == 7

    SchedulerHeartbeat.objects.update(pid=os.getpid() + 1)
    record_scheduler_heartbeat(schedules_fired=1)

    heartbeat = SchedulerHeartbeat.objects.get()
    assert heartbeat.pid == os.getpid()
    assert heartbeat.schedules_fired == 1


def test_staleness_is_unknown_before_the_first_tick():
    row, stale = heartbeat_state()

    assert row is None
    assert stale is None


def test_a_recent_tick_is_not_stale_and_an_old_one_is():
    SchedulerHeartbeat.objects.create(pid=os.getpid(), ticked_at=timezone.now())

    _, stale = heartbeat_state()
    assert stale is False

    SchedulerHeartbeat.objects.update(ticked_at=timezone.now() - timedelta(hours=1))
    _, stale = heartbeat_state()
    assert stale is True


def test_the_health_page_reports_the_tick_and_its_counters(operator):
    SchedulerHeartbeat.objects.create(
        pid=os.getpid(),
        ticked_at=timezone.now(),
        schedules_fired=4,
        expired_leases=1,
        recovered_jobs=2,
        campaigns_dispatched=3,
    )

    response = operator.get(reverse("jobs:service_health"))
    page = response.content.decode()

    assert response.status_code == 200
    assert "Schedules fired" in page
    assert "Campaigns dispatched" in page
    assert f"{os.getpid()}" in page
    assert "The loop is ticking at its expected interval" in page


def test_the_health_page_says_a_stale_tick_means_the_loop_stopped(operator):
    SchedulerHeartbeat.objects.create(
        pid=os.getpid(), ticked_at=timezone.now() - timedelta(hours=1)
    )

    page = operator.get(reverse("jobs:service_health")).content.decode()

    assert "No tick within the healthy window, so this loop is not running" in page


def test_the_health_page_reports_no_row_before_the_first_tick(operator):
    page = operator.get(reverse("jobs:service_health")).content.decode()

    assert "No scheduler tick has been recorded" in page
