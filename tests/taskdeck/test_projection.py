"""The projection must work under both task implementations.

The interesting case is the mismatched one: tasks defined with Django 6's
native ``django.tasks.task`` decorator, executed by ``django_tasks_db``, which
emits on the *backport's* signal objects. Binding to only ``django.tasks``
would leave the table empty while everything else appeared to work, so that
combination is asserted explicitly rather than assumed.
"""

import pytest
from django.core.management import call_command
from django.db import transaction
from django.test import override_settings

import taskdeck
from taskdeck.collector import collect_status
from taskdeck.models import TaskRun, TaskRunStatus
from tests.taskdeck import tasks

IMMEDIATE = {"default": {"BACKEND": "django_tasks.backends.immediate.ImmediateBackend"}}


def run_worker():
    """Drain the database queue in-process and return."""
    call_command("db_worker", "--batch", "--verbosity", "0")


@pytest.mark.django_db(transaction=True)
def test_enqueue_creates_queued_row():
    result = tasks.simple_ok.enqueue(3)

    run = TaskRun.objects.get(result_id=str(result.id))
    assert run.status == TaskRunStatus.QUEUED
    assert run.name == "simple_ok"
    assert run.project == "relay"
    # A root task correlates to itself, so every chain is queryable by one
    # column whether it began here or in another service.
    assert run.correlation_id == run.id
    assert run.parent_id is None


@pytest.mark.django_db(transaction=True)
def test_successful_run_reaches_terminal_state():
    result = tasks.simple_ok.enqueue(3)
    run_worker()

    run = TaskRun.objects.get(result_id=str(result.id))
    assert run.status == TaskRunStatus.SUCCESS
    assert run.started_at is not None
    assert run.finished_at is not None
    assert run.duration_s is not None
    assert run.error == ""


@pytest.mark.django_db(transaction=True)
def test_failed_run_records_error():
    result = tasks.simple_fail.enqueue()
    run_worker()

    run = TaskRun.objects.get(result_id=str(result.id))
    assert run.status == TaskRunStatus.FAILED
    assert run.finished_at is not None
    assert "RuntimeError" in run.error or "intentional failure" in run.error


@pytest.mark.django_db(transaction=True)
def test_fanout_links_children_and_derives_progress():
    parent_result = tasks.fanout.enqueue(3)
    run_worker()

    parent = TaskRun.objects.get(result_id=str(parent_result.id))
    children = TaskRun.objects.filter(parent=parent)

    assert children.count() == 3, "children must attach without explicit threading"
    assert parent.progress_total == 3

    # Children inherit the parent's correlation id, so the whole chain is one
    # query rather than a recursive walk.
    assert set(children.values_list("correlation_id", flat=True)) == {parent.correlation_id}

    parent.refresh_from_db()
    current, total = parent.resolved_progress()
    assert (current, total) == (3, 3), "numerator derives from finished children"


@pytest.mark.django_db(transaction=True)
def test_in_process_loop_reports_progress():
    result = tasks.loop_with_progress.enqueue(4)
    run_worker()

    run = TaskRun.objects.get(result_id=str(result.id))
    assert run.resolved_progress() == (4, 4)
    assert run.message == "item 4"


@pytest.mark.django_db(transaction=True)
def test_entity_and_owner_are_recorded():
    result = tasks.tagged.enqueue()
    run_worker()

    run = TaskRun.objects.get(result_id=str(result.id))
    assert (run.entity_type, run.entity_id) == ("campaign", "42")
    assert run.owner_id == "client-a"


@pytest.mark.django_db(transaction=True)
@override_settings(TASKS=IMMEDIATE)
def test_projection_also_works_on_the_native_immediate_backend():
    """Guards the dual binding from the other side.

    The immediate backend runs inline, so this exercises enqueue, start and
    finish through a different signal module than the database backend uses.
    """
    result = tasks.simple_ok.enqueue(5)

    run = TaskRun.objects.get(result_id=str(result.id))
    assert run.status == TaskRunStatus.SUCCESS
    assert run.started_at is not None


@pytest.mark.django_db(transaction=True)
def test_enqueue_on_commit_skips_enqueue_when_transaction_rolls_back():
    """The failure R8 exists to prevent: work queued for a state never committed."""
    class Rollback(Exception):
        pass

    with pytest.raises(Rollback), transaction.atomic():
        taskdeck.enqueue_on_commit(tasks.simple_ok.enqueue, 1)
        raise Rollback

    assert not TaskRun.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_enqueue_on_commit_enqueues_after_commit():
    with transaction.atomic():
        taskdeck.enqueue_on_commit(tasks.simple_ok.enqueue, 1)
        assert not TaskRun.objects.exists(), "must not enqueue before commit"

    assert TaskRun.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_collect_status_matches_the_contract_shape():
    tasks.simple_ok.enqueue(1)
    run_worker()

    payload = collect_status()

    assert payload["contract_version"] == 1
    assert payload["project"] == "relay"
    assert set(payload) >= {
        "contract_version",
        "project",
        "version",
        "generated_at",
        "worker",
        "queue",
        "schedules",
        "recent_runs",
        "failures_24h",
    }
    assert set(payload["worker"]) >= {"mode", "last_seen", "healthy"}
    assert set(payload["queue"]) == {"pending", "oldest_pending_age_s"}

    run = payload["recent_runs"][0]
    assert set(run) >= {
        "id",
        "correlation_id",
        "name",
        "status",
        "started",
        "duration_s",
        "progress",
        "message",
        "entity",
    }
    assert payload["failures_24h"] == 0


@pytest.mark.django_db(transaction=True)
def test_failures_are_counted_and_worker_reads_healthy_when_idle():
    tasks.simple_fail.enqueue()
    run_worker()

    payload = collect_status()
    assert payload["failures_24h"] == 1
    # A failed task is not a sick worker; only a running task that stops
    # checking in is.
    assert payload["worker"]["healthy"] is True
    assert payload["queue"]["pending"] == 0


@pytest.mark.django_db(transaction=True)
def test_stamp_annotates_a_task_that_is_only_queued():
    """Context must be visible before a worker picks the task up.

    Without this, a console shows a queue of anonymous rows exactly when the
    operator most wants to know what is waiting.
    """
    result = tasks.simple_ok.enqueue(1)
    taskdeck.stamp(result, entity=("campaign", "42"), owner_id="client-a", message="batch 1 of 3")

    run = TaskRun.objects.get(result_id=str(result.id))
    assert run.status == TaskRunStatus.QUEUED
    assert (run.entity_type, run.entity_id) == ("campaign", "42")
    assert run.owner_id == "client-a"
    assert run.message == "batch 1 of 3"


@pytest.mark.django_db(transaction=True)
def test_stamp_accepts_a_bare_result_id_and_ignores_unknown_ids():
    result = tasks.simple_ok.enqueue(1)
    assert taskdeck.stamp(str(result.id), owner_id="client-b") is True
    assert taskdeck.stamp("no-such-id", owner_id="client-b") is False
