"""Webhook task hardening rules (plan issue R1.2, relay issue #6).

Covers: the 60-second synchronous timeout ceiling, the 202 ack-then-callback
lease protocol, retry classes with exponential backoff, response-body
truncation to 2 KB, per-client concurrency and creation rate limits, the
`X-Relay-Attempt` header, the dead-letter ops view with retry, and replay of
an old timestamp rejected by the reference receiver documented in docs/api.md.
"""

import hashlib
import hmac
import io
import json
import urllib.error
from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client as DjangoClient
from django.utils import timezone

from jobs.models import Job, JobStatus, Schedule
from jobs.services import fail_expired_leases, submit_job
from jobs.tasks import execute_job
from mailing.models import Client, Organization
from mailing.services.auth import create_client_api_key

pytestmark = pytest.mark.django_db(transaction=True)

REPLAY_WINDOW_SECONDS = 300


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


class WebhookResponse:
    def __init__(self, status, body=b""):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self._body


def http_error(status, body=b""):
    return urllib.error.HTTPError(
        "https://client.example.com/work", status, "error", None, io.BytesIO(body)
    )


def submit_webhook(relay_client, key, **extra):
    request = {
        "type": "webhook",
        "idempotency_key": key,
        "url": "https://client.example.com/work",
        "params": {},
    }
    return submit_job(request | extra, relay_client)[0]


def run_worker():
    call_command("db_worker", "--batch", "--verbosity", "0")


def make_due(job):
    """Let a retrying job run again on the next worker sweep."""
    Job.objects.filter(pk=job.pk).update(run_after=timezone.now() - timedelta(seconds=1))
    job.refresh_from_db()


# --- Signed headers, attempt header ---------------------------------------


def test_webhook_headers_include_attempt_number(monkeypatch, relay_client):
    captured = []

    def fake_urlopen(request, timeout):
        captured.append(request.headers["X-relay-attempt"])
        if len(captured) == 1:
            raise http_error(429, b"slow down")
        return WebhookResponse(204, b"ok")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    job = submit_webhook(relay_client, "attempt-1", max_attempts=2)

    execute_job.call(str(job.pk))
    make_due(job)
    run_worker()
    execute_job.call(str(job.pk))
    job.refresh_from_db()

    assert captured == ["1", "2"]
    assert job.status == JobStatus.SUCCEEDED


def test_webhook_signature_and_task_headers_are_unchanged(monkeypatch, relay_client):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["headers"] = dict(request.headers)
        return WebhookResponse(200, b"ok")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    job = submit_webhook(relay_client, "headers-1")
    execute_job.call(str(job.pk))

    headers = captured["headers"]
    assert headers["X-relay-task-id"] == str(job.pk)
    assert headers["X-relay-correlation-id"] == str(job.correlation_id)
    assert "X-relay-timestamp" in headers
    assert headers["X-relay-signature"].startswith("sha256=")


# --- Retry classes ---------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_webhook_retries_429_and_5xx(monkeypatch, relay_client, status):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_, **__: (_ for _ in ()).throw(http_error(status)))
    job = submit_webhook(relay_client, f"retry-{status}")

    result = execute_job.call(str(job.pk))
    job.refresh_from_db()

    assert result["status"] == JobStatus.RETRYING
    assert job.attempt == 1
    assert job.response_status == status
    delay = (job.run_after - timezone.now()).total_seconds()
    assert 3 < delay <= settings.RELAY_JOB_RETRY_BASE_SECONDS + 2


@pytest.mark.parametrize("status", [400, 401, 404, 409, 410, 422, 408, 425])
def test_webhook_fails_immediately_on_other_4xx(monkeypatch, relay_client, status):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_, **__: (_ for _ in ()).throw(http_error(status)))
    job = submit_webhook(relay_client, f"permanent-{status}", max_attempts=5)

    result = execute_job.call(str(job.pk))
    job.refresh_from_db()

    assert result["status"] == JobStatus.FAILED
    assert job.attempt == 1
    assert job.response_status == status


def test_webhook_retries_transport_timeout(monkeypatch, relay_client):
    def slow_receiver(_request, **_kwargs):
        raise TimeoutError("receiver did not answer in time")

    monkeypatch.setattr("urllib.request.urlopen", slow_receiver)
    job = submit_webhook(relay_client, "timeout-1")

    result = execute_job.call(str(job.pk))
    job.refresh_from_db()

    assert result["status"] == JobStatus.RETRYING
    assert job.attempt == 1
    assert "receiver did not answer in time" in job.error


def test_webhook_retry_uses_exponential_backoff(monkeypatch, relay_client):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_, **__: (_ for _ in ()).throw(http_error(503)))
    job = submit_webhook(relay_client, "backoff-1", max_attempts=5)
    base = settings.RELAY_JOB_RETRY_BASE_SECONDS

    delays = []
    for attempt in range(1, 4):
        execute_job.call(str(job.pk))
        job.refresh_from_db()
        assert job.status == JobStatus.RETRYING
        delays.append((job.run_after - timezone.now()).total_seconds())
        make_due(job)

    expected = [base, base * 2, base * 4]
    for actual, want in zip(delays, expected, strict=True):
        assert want - 2 < actual <= want + 2


def test_webhook_default_max_attempts_is_five(monkeypatch, client, auth):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_, **__: (_ for _ in ()).throw(http_error(500)))
    response = client.post(
        "/api/tasks",
        data={
            "type": "webhook",
            "idempotency_key": "default-attempts",
            "url": "https://client.example.com/work",
            "params": {},
        },
        content_type="application/json",
        **auth,
    )
    assert response.status_code == 202
    assert response.json()["max_attempts"] == 5


# --- Response body truncation ---------------------------------------------


def test_webhook_failure_body_is_truncated_to_2kb(monkeypatch, relay_client):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_, **__: (_ for _ in ()).throw(http_error(400, b"x" * 5000)),
    )
    job = submit_webhook(relay_client, "truncate-1")

    execute_job.call(str(job.pk))
    job.refresh_from_db()

    assert job.status == JobStatus.FAILED
    assert len(job.response_body) == 2048


def test_webhook_success_body_is_truncated_to_2kb(monkeypatch, relay_client):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_, **__: WebhookResponse(200, b"y" * 5000),
    )
    job = submit_webhook(relay_client, "truncate-2")

    execute_job.call(str(job.pk))
    job.refresh_from_db()

    assert job.status == JobStatus.SUCCEEDED
    assert len(job.response_body) == 2048


# --- Ack-then-callback lease protocol --------------------------------------


def test_webhook_202_starts_a_lease_and_completion_succeeds_the_task(
    monkeypatch, relay_client, client, auth
):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_, **__: WebhookResponse(202, json.dumps({"lease_seconds": 120}).encode()),
    )
    job = submit_webhook(relay_client, "lease-1")
    execute_job.call(str(job.pk))
    job.refresh_from_db()

    assert job.status == JobStatus.RUNNING
    assert job.lease_expires_at is not None
    assert job.lease_expires_at > timezone.now()

    response = client.post(
        f"/api/tasks/{job.pk}/complete",
        data=json.dumps({"result": {"finished": True}}),
        content_type="application/json",
        **auth,
    )
    assert response.status_code == 200
    job.refresh_from_db()
    assert job.status == JobStatus.SUCCEEDED
    assert job.result == {"finished": True}
    assert job.lease_expires_at is None


def test_webhook_lease_serialized_to_clients(monkeypatch, relay_client, client, auth):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_, **__: WebhookResponse(202, json.dumps({"lease_seconds": 300}).encode()),
    )
    job = submit_webhook(relay_client, "lease-2")
    execute_job.call(str(job.pk))

    detail = client.get(f"/api/tasks/{job.pk}", **auth)
    assert detail.json()["status"] == "running"
    assert detail.json()["lease_expires_at"] is not None


def test_webhook_client_fail_callback_fails_the_task(
    monkeypatch, relay_client, client, auth
):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_, **__: WebhookResponse(202, json.dumps({"lease_seconds": 120}).encode()),
    )
    job = submit_webhook(relay_client, "lease-3")
    execute_job.call(str(job.pk))

    response = client.post(
        f"/api/tasks/{job.pk}/fail",
        data=json.dumps({"error": "worker died mid-task"}),
        content_type="application/json",
        **auth,
    )
    assert response.status_code == 200
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert job.error == "worker died mid-task"


@pytest.mark.parametrize(
    "body",
    [b"Accepted", json.dumps({"lease_seconds": 0}).encode(), json.dumps({"lease_seconds": 99999}).encode()],
)
def test_webhook_202_without_a_valid_lease_fails_the_task(monkeypatch, relay_client, body):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_, **__: WebhookResponse(202, body))
    job = submit_webhook(relay_client, f"bad-lease-{body[:8].decode('latin-1')}")

    result = execute_job.call(str(job.pk))
    job.refresh_from_db()

    assert result["status"] == JobStatus.FAILED
    assert "202" in job.error


def test_expired_lease_fails_the_task_and_sweep_is_terminal(relay_client):
    job = submit_webhook(relay_client, "expired-1")
    Job.objects.filter(pk=job.pk).update(
        status=JobStatus.RUNNING,
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )

    assert fail_expired_leases() == 1
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert "lease expired" in job.error
    assert fail_expired_leases() == 0


def test_scheduler_loop_sweeps_expired_leases(relay_client):
    job = submit_webhook(relay_client, "expired-2")
    Job.objects.filter(pk=job.pk).update(
        status=JobStatus.RUNNING,
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )

    call_command("run_relay_scheduler", "--once")
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED


def test_completion_after_lease_expiry_fails_the_task(
    monkeypatch, relay_client, client, auth
):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_, **__: WebhookResponse(202, json.dumps({"lease_seconds": 5}).encode()),
    )
    job = submit_webhook(relay_client, "expired-3")
    execute_job.call(str(job.pk))
    Job.objects.filter(pk=job.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1)
    )

    response = client.post(
        f"/api/tasks/{job.pk}/complete", data="{}", content_type="application/json", **auth
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "lease_expired"
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED


def test_callbacks_reject_conflicting_resolutions(
    monkeypatch, relay_client, client, auth
):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_, **__: WebhookResponse(202, json.dumps({"lease_seconds": 120}).encode()),
    )
    job = submit_webhook(relay_client, "conflict-1")
    execute_job.call(str(job.pk))

    ok = client.post(
        f"/api/tasks/{job.pk}/complete", data="{}", content_type="application/json", **auth
    )
    assert ok.status_code == 200
    repeat = client.post(
        f"/api/tasks/{job.pk}/complete", data="{}", content_type="application/json", **auth
    )
    assert repeat.status_code == 200
    assert repeat["X-Idempotent-Replay"] == "true"

    failed = client.post(
        f"/api/tasks/{job.pk}/fail",
        data=json.dumps({"error": "late failure"}),
        content_type="application/json",
        **auth,
    )
    assert failed.status_code == 409
    assert failed.json()["error"]["code"] == "task_already_finished"


def test_callback_on_queued_task_is_conflict(relay_client, client, auth):
    job = submit_webhook(relay_client, "queued-1")
    response = client.post(
        f"/api/tasks/{job.pk}/complete", data="{}", content_type="application/json", **auth
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_lease_in_progress"


def test_callbacks_are_scoped_to_the_owning_client(relay_client, client, auth):
    job = submit_webhook(relay_client, "scoped-1")
    Job.objects.filter(pk=job.pk).update(
        status=JobStatus.RUNNING,
        lease_expires_at=timezone.now() + timedelta(seconds=60),
    )

    other_org = Organization.objects.create(name="Other", slug="other-org")
    other_client = Client.objects.create(organization=other_org, name="Other", slug="other")
    _, other_key = create_client_api_key(client=other_client, name="other")

    response = client.post(
        f"/api/tasks/{job.pk}/complete",
        data="{}",
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {other_key}",
    )
    assert response.status_code == 404


def test_callbacks_require_post_and_authentication(client):
    assert client.post("/api/tasks/00000000-0000-0000-0000-000000000000/complete").status_code == 401
    assert client.get("/api/tasks/00000000-0000-0000-0000-000000000000/complete").status_code in {401, 405}


# --- Per-client concurrency and rate limits --------------------------------


def test_webhook_concurrency_limit_rejects_the_fifth_inflight_task(
    relay_client, client, auth
):
    for index in range(4):
        submit_webhook(relay_client, f"inflight-{index}")

    response = client.post(
        "/api/tasks",
        data={
            "type": "webhook",
            "idempotency_key": "inflight-5",
            "url": "https://client.example.com/work",
            "params": {},
        },
        content_type="application/json",
        **auth,
    )
    assert response.status_code == 429
    assert response.json()["error"]["fields"]["type"] == "concurrency_limit_exceeded"


def test_webhook_concurrency_frees_up_after_terminal_state(
    monkeypatch, relay_client, client, auth
):
    for index in range(4):
        job = submit_webhook(relay_client, f"freed-{index}")
    monkeypatch.setattr("urllib.request.urlopen", lambda *_, **__: WebhookResponse(204, b""))
    execute_job.call(str(job.pk))
    job.refresh_from_db()
    assert job.status == JobStatus.SUCCEEDED

    response = client.post(
        "/api/tasks",
        data={
            "type": "webhook",
            "idempotency_key": "freed-new",
            "url": "https://client.example.com/work",
            "params": {},
        },
        content_type="application/json",
        **auth,
    )
    assert response.status_code == 202


def test_webhook_rate_limit_rejects_creation_burst(
    relay_client, client, auth, monkeypatch
):
    monkeypatch.setattr(settings, "RELAY_WEBHOOK_RATE_LIMIT_PER_MINUTE", 2, raising=False)
    for index in range(2):
        response = client.post(
            "/api/tasks",
            data={
                "type": "webhook",
                "idempotency_key": f"burst-{index}",
                "url": "https://client.example.com/work",
                "params": {},
            },
            content_type="application/json",
            **auth,
        )
        assert response.status_code == 202

    third = client.post(
        "/api/tasks",
        data={
            "type": "webhook",
            "idempotency_key": "burst-2",
            "url": "https://client.example.com/work",
            "params": {},
        },
        content_type="application/json",
        **auth,
    )
    assert third.status_code == 429
    assert third.json()["error"]["fields"]["type"] == "rate_limit_exceeded"


def test_schedule_created_webhook_tasks_bypass_client_limits(relay_client):
    for index in range(4):
        submit_webhook(relay_client, f"limit-{index}")

    schedule = Schedule.objects.create(
        client=relay_client,
        name="refresh",
        cron="* * * * *",
        task_type="webhook",
        task={
            "url": "https://client.example.com/work",
            "payload": {},
            "timeout_seconds": 30.0,
        },
        max_attempts=3,
        next_run_at=timezone.now(),
    )
    job, created = submit_job(
        {
            "type": "webhook",
            "idempotency_key": "scheduled-refresh",
            "url": "https://client.example.com/work",
            "params": {},
        },
        relay_client,
        schedule=schedule,
    )
    assert created
    assert Job.objects.filter(pk=job.pk).exists()


# --- Dead-letter list in ops views -----------------------------------------


@pytest.fixture
def staff_client():
    user = get_user_model().objects.create_user(
        username="operator", password="pw-123456789", is_staff=True, is_active=True
    )
    django_client = DjangoClient()
    django_client.force_login(user)
    return django_client


def test_webhook_dead_letter_list_shows_failed_tasks_with_retry(
    monkeypatch, relay_client, staff_client
):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_, **__: (_ for _ in ()).throw(http_error(400)))
    job = submit_webhook(relay_client, "dead-1")
    execute_job.call(str(job.pk))
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED

    response = staff_client.get("/jobs/dead-letters/")
    assert response.status_code == 200
    assert str(job.pk) in response.content.decode()
    assert f"/jobs/dead-letters/{job.pk}/retry/" in response.content.decode()


def test_webhook_dead_letter_retry_requeues_the_task(
    monkeypatch, relay_client, staff_client
):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_, **__: (_ for _ in ()).throw(http_error(400)))
    job = submit_webhook(relay_client, "dead-2")
    execute_job.call(str(job.pk))
    assert Job.objects.get(pk=job.pk).status == JobStatus.FAILED

    response = staff_client.post(f"/jobs/dead-letters/{job.pk}/retry/")
    assert response.status_code == 302

    job.refresh_from_db()
    assert job.status == JobStatus.QUEUED
    assert job.attempt == 0
    assert job.error == ""


def test_webhook_dead_letter_views_need_staff(relay_client, client):
    response = client.get("/jobs/dead-letters/")
    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"] or "/auth/login" in response.headers["Location"]


# --- Reference receiver (docs/api.md) --------------------------------------


class ReceiverRejection(Exception):
    pass


def reference_receiver_verify(body, headers, secret, *, now):
    """Verbatim mirror of the reference receiver documented in docs/api.md."""
    timestamp = headers.get("X-Relay-Timestamp", "")
    signature = headers.get("X-Relay-Signature", "")
    try:
        stamp = int(timestamp)
    except ValueError:
        raise ReceiverRejection("missing or malformed timestamp") from None
    if abs(now - stamp) > REPLAY_WINDOW_SECONDS:
        raise ReceiverRejection("timestamp outside replay window")
    expected = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest("sha256=" + expected, signature):
        raise ReceiverRejection("bad signature")


def sign_relay_request(payload, secret, timestamp):
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()
    return body, {
        "X-Relay-Timestamp": str(timestamp),
        "X-Relay-Signature": f"sha256={signature}",
    }


def test_webhook_reference_receiver_accepts_a_fresh_signed_request():
    now = int(timezone.now().timestamp())
    body, headers = sign_relay_request({"course": "ml-zoomcamp"}, "webhook-secret", now)

    reference_receiver_verify(body, headers, "webhook-secret", now=now)


def test_webhook_reference_receiver_rejects_a_replayed_old_timestamp():
    now = int(timezone.now().timestamp())
    replayed_at = now - 2 * REPLAY_WINDOW_SECONDS
    body, headers = sign_relay_request({"course": "ml-zoomcamp"}, "webhook-secret", replayed_at)

    with pytest.raises(ReceiverRejection, match="replay window"):
        reference_receiver_verify(body, headers, "webhook-secret", now=now)


def test_webhook_reference_receiver_rejects_a_forged_signature():
    now = int(timezone.now().timestamp())
    body, headers = sign_relay_request({"course": "ml-zoomcamp"}, "webhook-secret", now)
    headers["X-Relay-Signature"] = "sha256=" + "0" * 64

    with pytest.raises(ReceiverRejection, match="bad signature"):
        reference_receiver_verify(body, headers, "webhook-secret", now=now)
