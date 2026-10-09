"""The status contract endpoint.

Shape is asserted against docs/contract.md in the taskdeck repo, because the
cross-project console depends on it and the two deploy independently.
"""

import datetime
import logging
import os
import uuid

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from jobs.models import SchedulerHeartbeat
from mailing.models import (
    Audience,
    Campaign,
    CampaignStatus,
    Client,
    Contact,
    Organization,
    Subscription,
    SubscriptionStatus,
)
from mailing.ops_views import BATCH_TASK_PATH
from mailing.services.campaigns import queue_campaign
from taskdeck.models import TaskRun, TaskRunStatus

pytestmark = pytest.mark.django_db

TOKEN = "test-status-token"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="A", slug="a")


@pytest.fixture
def client_record(organization):
    return Client.objects.create(organization=organization, name="C", slug="c")


@pytest.fixture
def campaign(audience, client_record):
    contact = Contact.objects.create(
        email="person@example.com", verified_at=timezone.now()
    )
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=client_record,
        status=SubscriptionStatus.SUBSCRIBED,
    )
    return Campaign.objects.create(
        audience=audience,
        client=client_record,
        subject="August newsletter",
        html_body="<p>Hi</p>",
        status=CampaignStatus.DRAFT,
    )


def url():
    return reverse("mailing:taskdeck_status")


def test_returns_404_when_no_token_is_configured(client, settings):
    """An unconfigured host must not expose operational detail at all."""
    settings.TASKDECK_STATUS_TOKEN = ""
    assert client.get(url(), headers={"authorization": "Bearer anything"}).status_code == 404


def test_returns_404_for_a_wrong_token(client, settings):
    settings.TASKDECK_STATUS_TOKEN = TOKEN
    response = client.get(url(), headers={"authorization": "Bearer wrong"})
    # 404 rather than 401, so a caller cannot probe which hosts serve this.
    assert response.status_code == 404


def test_returns_404_without_an_authorization_header(client, settings):
    settings.TASKDECK_STATUS_TOKEN = TOKEN
    assert client.get(url()).status_code == 404


def test_payload_matches_the_contract(client, settings):
    settings.TASKDECK_STATUS_TOKEN = TOKEN

    response = client.get(url(), headers={"authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 200
    payload = response.json()
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


def test_ingress_backlogs_are_reported_alongside_task_state(client, settings):
    """A backed-up SES notification queue must not look like a healthy system.

    Those queues are fed by AWS directly and are invisible to the task system,
    so the project adds them to the payload itself.
    """
    settings.TASKDECK_STATUS_TOKEN = TOKEN

    payload = client.get(url(), headers={"authorization": f"Bearer {TOKEN}"}).json()

    assert "ingress_backlogs" in payload
    assert all({"name", "label", "count"} <= set(row) for row in payload["ingress_backlogs"])


def test_runs_carry_a_resolved_entity_link(
    client, settings, campaign, django_capture_on_commit_callbacks
):
    settings.TASKDECK_STATUS_TOKEN = TOKEN
    with django_capture_on_commit_callbacks(execute=True):
        queue_campaign(campaign)

    payload = client.get(url(), headers={"authorization": f"Bearer {TOKEN}"}).json()

    runs = [r for r in payload["recent_runs"] if r["name"] == "send_campaign_email_batch"]
    assert runs, "the queued batch should appear in recent runs"
    assert runs[0]["entity"] == {
        "label": "August newsletter",
        "url": reverse("mailing:campaign_detail", args=[campaign.id]),
    }


def test_a_broken_entity_resolver_degrades_to_no_link(
    client, settings, campaign, monkeypatch, django_capture_on_commit_callbacks
):
    """A resolver touching project models may raise; the endpoint must survive."""
    settings.TASKDECK_STATUS_TOKEN = TOKEN
    with django_capture_on_commit_callbacks(execute=True):
        queue_campaign(campaign)

    def boom(*args, **kwargs):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr("mailing.ops_views._entity_resolver", boom)

    response = client.get(url(), headers={"authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 200
    runs = [r for r in response.json()["recent_runs"] if r["name"] == "send_campaign_email_batch"]
    assert runs[0]["entity"] is None


def test_response_is_cached_so_polling_is_cheap(client, settings, monkeypatch):
    settings.TASKDECK_STATUS_TOKEN = TOKEN
    calls = []
    real = __import__("mailing.ops_views", fromlist=["build_payload"]).build_payload

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr("mailing.ops_views.build_payload", counting)

    headers = {"authorization": f"Bearer {TOKEN}"}
    client.get(url(), headers=headers)
    client.get(url(), headers=headers)

    assert len(calls) == 1, "second poll within the window must be served from cache"


SCHEDULE = [
    {
        "name": "deadline-reminders",
        "template_key": "deadline-reminder",
        "cron": "0 9 * * *",
        "max_age_s": 93600,
    }
]


def _batch_run(message, status, finished_at=None):
    return TaskRun.objects.create(
        result_id=f"r-{TaskRun.objects.count()}",
        project="datamailer",
        name="send_transactional_email_batch",
        func="mailing.tasks.send_transactional_email_batch",
        status=status,
        message=message,
        correlation_id=uuid.uuid4(),
        finished_at=finished_at,
    )


def test_schedule_with_no_run_reports_nulls(client, settings):
    """The case that matters: a schedule that has never fired, or has stopped,
    must be distinguishable from one that is simply quiet."""
    settings.TASKDECK_STATUS_TOKEN = "t"
    settings.TASKDECK_SCHEDULES = SCHEDULE
    cache.clear()

    body = client.get(url(), headers={"authorization": "Bearer t"}).json()

    assert len(body["schedules"]) == 1
    schedule = body["schedules"][0]
    assert schedule["name"] == "deadline-reminders"
    assert schedule["cron"] == "0 9 * * *"
    assert schedule["last_run"] is None
    assert schedule["last_success"] is None
    assert schedule["max_age_s"] == 93600


def test_schedule_reports_its_last_run_and_last_success(client, settings):
    settings.TASKDECK_STATUS_TOKEN = "t"
    settings.TASKDECK_SCHEDULES = SCHEDULE
    cache.clear()

    finished = timezone.now() - datetime.timedelta(hours=2)
    _batch_run("deadline-reminder: 40 recipients", TaskRunStatus.SUCCESS, finished_at=finished)
    # A later failure must not overwrite last_success -- that distinction is
    # what says "it is still running but no longer working".
    _batch_run("deadline-reminder: 41 recipients", TaskRunStatus.FAILED)

    body = client.get(url(), headers={"authorization": "Bearer t"}).json()
    schedule = body["schedules"][0]

    assert schedule["last_run"] is not None
    assert schedule["last_success"] == finished.isoformat()


def test_schedule_ignores_batches_for_a_different_template(client, settings):
    """A scoring run must not be mistaken for a reminder sweep."""
    settings.TASKDECK_STATUS_TOKEN = "t"
    settings.TASKDECK_SCHEDULES = SCHEDULE
    cache.clear()

    _batch_run("homework-score-notification: 12 recipients", TaskRunStatus.SUCCESS,
               finished_at=timezone.now())

    body = client.get(url(), headers={"authorization": "Bearer t"}).json()

    assert body["schedules"][0]["last_run"] is None


def test_next_run_is_resolved_from_the_declared_cron(client, settings):
    """A consumer must be able to answer "when is the next one due".

    The value is the next occurrence after the payload is built, so it is
    compared as a timestamp rather than a fixed string: a slow test run must
    not pass a check that happens to agree with a stale expectation.
    """
    settings.TASKDECK_STATUS_TOKEN = "t"
    settings.TASKDECK_SCHEDULES = SCHEDULE
    cache.clear()

    before = timezone.now()
    body = client.get(url(), headers={"authorization": "Bearer t"}).json()
    next_run = datetime.datetime.fromisoformat(body["schedules"][0]["next_run"])
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=datetime.UTC)

    assert before < next_run <= before + datetime.timedelta(days=1)
    # "0 9 * * *" fires at 09:00, so the resolved occurrence carries that wall
    # time rather than collapsing onto the moment the payload was built.
    assert next_run.hour == 9
    assert next_run.minute == 0


def test_next_run_is_null_for_an_unparseable_cron(client, settings, caplog):
    """One bad declaration must not 500 the whole status endpoint.

    The failure is surfaced in the log with the expression attached, so the
    operator can see which declared schedule is broken; the row reports null so
    a consumer renders it as unknown rather than trusting a zero value.
    """
    settings.TASKDECK_STATUS_TOKEN = "t"
    settings.TASKDECK_SCHEDULES = [{"name": "broken", "template_key": "broken", "cron": "not a cron"}]
    cache.clear()

    with caplog.at_level(logging.WARNING, logger="mailing.ops_views"):
        response = client.get(url(), headers={"authorization": "Bearer t"})

    assert response.status_code == 200
    assert response.json()["schedules"][0]["next_run"] is None
    assert "not a cron" in caplog.text


def test_next_run_is_null_for_an_absent_cron(client, settings):
    """A declared schedule with no expression has no next occurrence to report."""
    settings.TASKDECK_STATUS_TOKEN = "t"
    settings.TASKDECK_SCHEDULES = [{"name": "quiet", "template_key": "quiet"}]
    cache.clear()

    response = client.get(url(), headers={"authorization": "Bearer t"})

    assert response.status_code == 200
    assert response.json()["schedules"][0]["next_run"] is None


def test_next_run_is_null_for_a_non_string_cron(client, settings, caplog):
    """Settings values skip the API validator, so the endpoint must survive junk.

    A mistyped declaration (an int where a string belongs) makes croniter raise
    AttributeError rather than ValueError; either way the row reports null.
    """
    settings.TASKDECK_STATUS_TOKEN = "t"
    settings.TASKDECK_SCHEDULES = [{"name": "mistyped", "template_key": "mistyped", "cron": 9}]
    cache.clear()

    with caplog.at_level(logging.WARNING, logger="mailing.ops_views"):
        response = client.get(url(), headers={"authorization": "Bearer t"})

    assert response.status_code == 200
    assert response.json()["schedules"][0]["next_run"] is None
    assert "9" in caplog.text


def test_a_fresh_scheduler_tick_makes_the_worker_read_healthy(client, settings):
    """A scheduler that ticks is the worker's liveness, not a running task's.

    With no task heartbeats at all the payload used to have no ``last_seen``;
    the scheduler's own pass is a stronger signal than nothing, so it is fused
    into the same field rather than reported beside it.
    """
    settings.TASKDECK_STATUS_TOKEN = TOKEN
    ticked = timezone.now()
    SchedulerHeartbeat.objects.create(pid=os.getpid(), ticked_at=ticked)

    payload = client.get(url(), headers={"authorization": f"Bearer {TOKEN}"}).json()

    assert payload["worker"]["last_seen"] == ticked.isoformat()
    assert payload["worker"]["scheduler_last_tick"] == ticked.isoformat()
    assert payload["worker"]["healthy"] is True


def test_a_stale_scheduler_tick_makes_the_worker_read_unhealthy(client, settings):
    """The case the heartbeat exists for.

    A loop that stopped is indistinguishable from a quiet period in every other
    signal this payload carries, so staleness has to be a fault on its own.
    """
    settings.TASKDECK_STATUS_TOKEN = TOKEN
    SchedulerHeartbeat.objects.create(
        pid=os.getpid(), ticked_at=timezone.now() - datetime.timedelta(hours=1)
    )

    payload = client.get(url(), headers={"authorization": f"Bearer {TOKEN}"}).json()

    assert payload["worker"]["healthy"] is False
    assert payload["worker"]["scheduler_last_tick"] is not None


def test_no_tick_at_all_leaves_a_task_only_worker_healthy(client, settings):
    """A host with no scheduler row is not a host with a dead scheduler.

    Judging it unhealthy would report a fault on every project that does not
    run schedules at all.
    """
    settings.TASKDECK_STATUS_TOKEN = TOKEN

    payload = client.get(url(), headers={"authorization": f"Bearer {TOKEN}"}).json()

    assert payload["worker"]["scheduler_last_tick"] is None
    assert payload["worker"]["healthy"] is True


def test_an_old_task_heartbeat_alone_does_not_make_the_worker_read_unhealthy(client, settings):
    """A quiet afternoon is not a sick worker.

    A task only has to check in while it is running; a finished one from hours
    ago is exactly what a healthy idle system looks like. Only the scheduler,
    which must tick whether or not anything was due, is judged on staleness.
    """
    settings.TASKDECK_STATUS_TOKEN = TOKEN
    TaskRun.objects.create(
        result_id="quiet-run",
        project="datamailer",
        name="send_transactional_email_batch",
        func=BATCH_TASK_PATH,
        status=TaskRunStatus.SUCCESS,
        message="deadline-reminder: 40 recipients",
        correlation_id=uuid.uuid4(),
        heartbeat_at=timezone.now() - datetime.timedelta(hours=6),
        finished_at=timezone.now() - datetime.timedelta(hours=6),
    )

    payload = client.get(url(), headers={"authorization": f"Bearer {TOKEN}"}).json()

    assert payload["worker"]["last_seen"] is not None
    assert payload["worker"]["healthy"] is True
