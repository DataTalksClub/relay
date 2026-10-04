from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from jobs.models import Job, JobStatus, Schedule
from jobs.operator_actions import OperationConflict, change_schedule_state, retry_failed_job
from mailing.models import Client, Organization

pytestmark = pytest.mark.django_db


@pytest.fixture
def operator(client):
    client.force_login(get_user_model().objects.create_user(username="operations", is_staff=True))
    return client


@pytest.fixture
def workspace():
    org = Organization.objects.create(name="Operations", slug="operations")
    return Client.objects.create(name="Integration A", slug="integration-a", organization=org)


def make_job(workspace, **kwargs):
    return Job.objects.create(client=workspace, task_type="system.echo", idempotency_key="test", request_hash="hash", **kwargs)


def make_schedule(workspace, **kwargs):
    return Schedule.objects.create(client=workspace, name="hourly", cron="0 * * * *", task_type="system.echo", task={"params": {}}, next_run_at=timezone.now() - timedelta(hours=2), **kwargs)


def confirmation(record):
    return {"record_client_id": str(record.client_id), "revision": record.updated_at.isoformat(), "confirmed": "yes"}


def test_operations_pages_require_staff(client, workspace):
    job = make_job(workspace)
    schedule = make_schedule(workspace)
    for url in ["/jobs/", "/jobs/health/", "/jobs/schedules/", f"/jobs/{job.pk}/", f"/jobs/schedules/{schedule.pk}/"]:
        assert client.get(url).status_code == 302


def test_jobs_filter_and_global_identity(operator, workspace):
    other = Client.objects.create(name="Integration B", slug="integration-b", organization=workspace.organization)
    make_job(workspace, status=JobStatus.FAILED, error="receiver unavailable")
    Job.objects.create(client=other, task_type="webhook", idempotency_key="other", request_hash="hash")
    response = operator.get("/jobs/", {"client": workspace.pk, "status": "failed", "q": "receiver"})
    assert response.status_code == 200
    assert len(response.context["page"]) == 1
    assert response.context["page"][0].client == workspace
    assert "Shared service" in response.content.decode()


def test_retry_requires_matching_confirmation_and_failed_state(operator, workspace):
    job = make_job(workspace, status=JobStatus.FAILED, attempt=5, error="lost callback", finished_at=timezone.now())
    url = f"/jobs/{job.pk}/retry/"
    with patch("jobs.operator_actions.enqueue_job") as enqueue:
        operator.post(url, {"confirmed": "yes"})
        job.refresh_from_db()
        assert job.status == JobStatus.FAILED
        with TestCase.captureOnCommitCallbacks(execute=True):
            operator.post(url, confirmation(job))
        job.refresh_from_db()
        assert job.status == JobStatus.QUEUED
        assert job.attempt == 0
        assert job.finished_at is None
        assert job.error == ""
        enqueue.assert_called_once_with(job.pk)
        operator.post(url, confirmation(job))
        enqueue.assert_called_once()


def test_schedule_pause_resume_does_not_run_work(operator, workspace):
    schedule = make_schedule(workspace)
    url = f"/jobs/schedules/{schedule.pk}/action/"
    response = operator.post(url, confirmation(schedule) | {"action": "pause"}, follow=True)
    assert response.status_code == 200
    assert response.resolver_match.view_name == "jobs:schedule_detail_ui"
    assert "text/html" in response["Content-Type"]
    schedule.refresh_from_db()
    assert not schedule.enabled
    assert not Job.objects.exists()
    response = operator.post(url, confirmation(schedule) | {"action": "resume"}, follow=True)
    assert response.status_code == 200
    assert response.resolver_match.view_name == "jobs:schedule_detail_ui"
    schedule.refresh_from_db()
    assert schedule.enabled
    assert schedule.next_run_at > timezone.now()
    assert not Job.objects.exists()


def test_schedule_rejects_stale_or_wrong_client_confirmation(operator, workspace):
    schedule = make_schedule(workspace)
    stale = confirmation(schedule)
    Schedule.objects.filter(pk=schedule.pk).update(updated_at=timezone.now() + timedelta(seconds=1))
    url = f"/jobs/schedules/{schedule.pk}/action/"
    operator.post(url, stale | {"action": "pause"})
    schedule.refresh_from_db()
    assert schedule.enabled
    operator.post(url, confirmation(schedule) | {"action": "pause", "record_client_id": "999"})
    schedule.refresh_from_db()
    assert schedule.enabled


def test_operations_details_render_revision_and_policy(operator, workspace):
    schedule = make_schedule(workspace)
    job = make_job(workspace, status=JobStatus.FAILED, schedule=schedule)
    response = operator.get(f"/jobs/{job.pk}/")
    assert job.updated_at.isoformat() in response.content.decode()
    response = operator.get(f"/jobs/schedules/{schedule.pk}/")
    assert schedule.updated_at.isoformat() in response.content.decode()
    assert "does not replay every missed tick" in response.content.decode()


def test_health_unavailable_is_not_reported_healthy(operator, settings):
    settings.WORKER_STATUS_SYSTEMD_ENABLED = False
    with patch("mailing.services.worker_status.backlog_count", return_value=None):
        response = operator.get("/jobs/health/")
    assert response.status_code == 200
    assert "Processing health unavailable" in response.content.decode()
    assert "Unknown or unavailable monitoring does not confirm healthy processing" in response.content.decode()


def test_shared_schedule_operation_requires_boolean_confirmation(workspace):
    schedule = make_schedule(workspace)
    with pytest.raises(OperationConflict):
        change_schedule_state(schedule.pk, "pause", client_id=workspace.pk,
                              revision=schedule.updated_at.isoformat(), confirmed="yes")
    schedule.refresh_from_db()
    assert schedule.enabled
    updated = change_schedule_state(schedule.pk, "pause", client_id=workspace.pk,
                                    revision=schedule.updated_at.isoformat(), confirmed=True)
    assert not updated.enabled
    assert not Job.objects.exists()


def test_shared_retry_rejects_running_job(workspace):
    job = make_job(workspace, status=JobStatus.RUNNING)
    with patch("jobs.operator_actions.enqueue_job") as enqueue:
        with pytest.raises(OperationConflict):
            retry_failed_job(job.pk, client_id=workspace.pk,
                             revision=job.updated_at.isoformat(), confirmed=True)
        enqueue.assert_not_called()
    job.refresh_from_db()
    assert job.status == JobStatus.RUNNING


def test_invalid_client_filter_does_not_widen_results(operator, workspace):
    make_job(workspace)
    make_schedule(workspace)
    for url in ["/jobs/", "/jobs/schedules/"]:
        response = operator.get(url, {"client": "not-a-client"})
        assert len(response.context["page"]) == 0
        assert "That client filter is invalid" in response.content.decode()


def test_job_return_preserves_filters_and_rejects_external_links(operator, workspace):
    job = make_job(workspace, status=JobStatus.FAILED)
    context_url = f"/jobs/?client={workspace.pk}&status=failed&q=receiver&page=2"
    response = operator.get(f"/jobs/{job.pk}/", {"return": context_url})
    assert response.context["return_url"] == context_url
    response = operator.post(f"/jobs/{job.pk}/retry/", {"return": context_url}, follow=True)
    assert response.context["return_url"] == context_url
    assert "role=\"alert\"" in response.content.decode()
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    for unsafe in ["https://outside.example/jobs/", "//outside.example/jobs/", "/contacts/", "\\\\outside.example/jobs/"]:
        response = operator.get(f"/jobs/{job.pk}/", {"return": unsafe})
        assert response.context["return_url"] == f"/jobs/?client={workspace.pk}"


def test_invalid_cron_is_recoverable_resume_error(operator, workspace):
    schedule = make_schedule(workspace, enabled=False)
    schedule.cron = "invalid cron"
    schedule.save()
    response = operator.post(f"/jobs/schedules/{schedule.pk}/action/",
                             confirmation(schedule) | {"action": "resume"}, follow=True)
    assert response.status_code == 200
    assert "invalid run policy" in response.content.decode()
    assert "role=\"alert\"" in response.content.decode()
    schedule.refresh_from_db()
    assert not schedule.enabled
    assert not Job.objects.exists()


def test_retry_transport_error_leaves_recoverable_queued_job(operator, workspace):
    job = make_job(workspace, status=JobStatus.FAILED)
    with patch("jobs.operator_actions.enqueue_job", side_effect=RuntimeError("queue unavailable")):
        with TestCase.captureOnCommitCallbacks(execute=True):
            response = operator.post(f"/jobs/{job.pk}/retry/", confirmation(job), follow=True)
    assert response.status_code == 200
    assert "Job queued for a new attempt" in response.content.decode()
    job.refresh_from_db()
    assert job.status == JobStatus.QUEUED
    assert job.task_result_id == ""
