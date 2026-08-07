"""The status contract endpoint.

Shape is asserted against docs/contract.md in the taskdeck repo, because the
cross-project console depends on it and the two deploy independently.
"""

import datetime
import uuid

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from taskdeck.models import TaskRun, TaskRunStatus

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
from mailing.services.campaigns import queue_campaign

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
    assert payload["project"] == "datamailer"
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
