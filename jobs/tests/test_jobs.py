import hashlib
import hmac
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from jobs.models import JobStatus, Schedule
from jobs.scheduling import run_due_schedules, upsert_schedule
from jobs.services import submit_job
from jobs.tasks import execute_job
from mailing.models import Client, Organization
from mailing.services.auth import create_client_api_key
from taskdeck.models import TaskRun

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def relay_client():
    organization = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    return Client.objects.create(
        organization=organization,
        name="Courses",
        slug="dtc-courses",
        relay_webhook_signing_secret="webhook-secret",
        relay_webhook_allowed_origins=["https://client.example.com"],
    )


@pytest.fixture
def auth(relay_client):
    _, raw_key = create_client_api_key(client=relay_client, name="test")
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def run_worker():
    call_command("db_worker", "--batch", "--verbosity", "0")


def test_task_api_requires_authentication(client):
    response = client.get("/api/tasks")
    assert response.status_code == 401


def test_echo_submission_is_idempotent_and_runs_through_worker(client, auth, relay_client):
    request = {
        "type": "system.echo",
        "idempotency_key": "echo-1",
        "correlation_id": "39d40880-e4c6-43b1-ab71-72100c98bf2e",
        "params": {"hello": "relay"},
    }
    first = client.post("/api/tasks", data=request, content_type="application/json", **auth)
    second = client.post("/api/tasks", data=request, content_type="application/json", **auth)

    assert first.status_code == 202
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["idempotent_replay"] is True

    run_worker()

    detail = client.get(f"/api/tasks/{first.json()['id']}", **auth)
    assert detail.status_code == 200
    assert detail.json()["status"] == "succeeded"
    assert detail.json()["result"] == {"hello": "relay"}
    run = TaskRun.objects.get(entity_type="relay_job", entity_id=first.json()["id"])
    assert run.owner_id == str(relay_client.pk)


def test_idempotency_key_reuse_with_different_work_is_rejected(client, auth):
    first = {
        "type": "system.echo",
        "idempotency_key": "same-key",
        "params": {"value": 1},
    }
    second = first | {"params": {"value": 2}}
    assert client.post("/api/tasks", data=first, content_type="application/json", **auth).status_code == 202
    response = client.post("/api/tasks", data=second, content_type="application/json", **auth)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"


def test_task_queries_are_tenant_scoped(client, auth, relay_client):
    other_org = Organization.objects.create(name="Other", slug="other")
    other_client = Client.objects.create(organization=other_org, name="Other", slug="other")
    other_job, _ = submit_job(
        {"type": "system.echo", "idempotency_key": "private", "params": {}},
        other_client,
    )

    assert client.get(f"/api/tasks/{other_job.pk}", **auth).status_code == 404
    response = client.get("/api/tasks", **auth)
    assert all(item["id"] != str(other_job.pk) for item in response.json()["tasks"])
    assert relay_client.pk != other_client.pk


def test_webhook_is_signed_and_response_is_recorded(monkeypatch, relay_client):
    captured = {}

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b"accepted"

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    job, _ = submit_job(
        {
            "type": "webhook",
            "idempotency_key": "webhook-1",
            "url": "https://client.example.com/internal/rebuild",
            "params": {"course": "ml-zoomcamp"},
            "timeout_seconds": 12,
        },
        relay_client,
    )

    result = execute_job.call(str(job.pk))
    job.refresh_from_db()

    assert result["status"] == JobStatus.SUCCEEDED
    assert job.response_status == 204
    assert job.response_body == "accepted"
    assert captured["timeout"] == 12
    request = captured["request"]
    timestamp = request.headers["X-relay-timestamp"]
    expected = hmac.new(
        b"webhook-secret",
        timestamp.encode() + b"." + request.data,
        hashlib.sha256,
    ).hexdigest()
    assert request.headers["X-relay-signature"] == f"sha256={expected}"
    assert request.headers["X-relay-task-id"] == str(job.pk)


def test_webhook_origin_must_be_registered(client, auth):
    response = client.post(
        "/api/tasks",
        data={
            "type": "webhook",
            "idempotency_key": "blocked",
            "url": "https://untrusted.example.net/work",
            "params": {},
        },
        content_type="application/json",
        **auth,
    )
    assert response.status_code == 400
    assert response.json()["error"]["fields"]["url"] == "origin_not_allowed"


def test_retryable_webhook_failure_is_rescheduled(monkeypatch, relay_client):
    def fail_urlopen(_request, _timeout):
        raise TimeoutError("slow client")

    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)
    job, _ = submit_job(
        {
            "type": "webhook",
            "idempotency_key": "retry-1",
            "url": "https://client.example.com/work",
            "params": {},
            "max_attempts": 2,
        },
        relay_client,
    )
    result = execute_job.call(str(job.pk))
    job.refresh_from_db()

    assert result["status"] == JobStatus.RETRYING
    assert job.attempt == 1
    assert job.run_after > timezone.now()
    assert job.task_result_id


def test_email_send_is_a_builtin_job(monkeypatch, relay_client):
    monkeypatch.setattr(
        "jobs.tasks.send_transactional_email_for_client",
        lambda params, client: {"message": {"id": 42}, "enqueued": True},
    )
    job, _ = submit_job(
        {
            "type": "email.send",
            "idempotency_key": "mail-job-1",
            "params": {"email": "learner@example.com", "template_key": "welcome"},
        },
        relay_client,
    )
    execute_job.call(str(job.pk))
    job.refresh_from_db()
    assert job.status == JobStatus.SUCCEEDED
    assert job.result == {"message": {"id": 42}, "enqueued": True}


def test_due_schedule_creates_one_job_and_tracks_success(relay_client):
    schedule, _ = upsert_schedule(
        {
            "name": "heartbeat",
            "cron": "* * * * *",
            "type": "system.echo",
            "params": {"scheduled": True},
        },
        relay_client,
    )
    planned = timezone.now() - timedelta(minutes=2)
    Schedule.objects.filter(pk=schedule.pk).update(next_run_at=planned)

    fired = run_due_schedules(now=timezone.now())
    schedule.refresh_from_db()

    assert len(fired) == 1
    assert fired[0].schedule_id == schedule.pk
    assert schedule.last_job_id == fired[0].pk
    assert schedule.last_missed_at == planned
    assert schedule.next_run_at > planned

    execute_job.call(str(fired[0].pk))
    schedule.refresh_from_db()
    assert schedule.last_success_at is not None


def test_schedule_api_lists_only_authenticated_clients_schedule(client, auth):
    response = client.post(
        "/api/schedules",
        data={
            "name": "daily-echo",
            "cron": "0 9 * * *",
            "type": "system.echo",
            "params": {"daily": True},
        },
        content_type="application/json",
        **auth,
    )
    assert response.status_code == 201
    listing = client.get("/api/schedules", **auth)
    assert [item["name"] for item in listing.json()["schedules"]] == ["daily-echo"]


def test_submission_requires_idempotency_key(client, auth):
    response = client.post(
        "/api/tasks",
        data={"type": "system.echo", "params": {}},
        content_type="application/json",
        **auth,
    )
    assert response.status_code == 400
    assert response.json()["error"]["fields"]["idempotency_key"] == "required"
