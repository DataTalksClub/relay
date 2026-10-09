"""Confirmed staff operations shared by HTML and admin JSON endpoints."""

from django.db import transaction
from django.utils import timezone

from jobs.models import Job, JobStatus, Schedule
from jobs.scheduling import next_occurrence
from jobs.services import enqueue_job


class OperationConflict(ValueError):
    """An operation needs a fresh record review or explicit confirmation."""


def _require_confirmation(record, *, client_id, revision, confirmed):
    if (
        confirmed is not True
        or str(client_id) != str(record.client_id)
        or revision != record.updated_at.isoformat()
    ):
        raise OperationConflict("The record changed or the confirmation is incomplete. Review it again.")


def retry_failed_job(job_id, *, client_id, revision, confirmed):
    """Queue one failed job after checking the reviewed revision under lock."""
    with transaction.atomic():
        job = Job.objects.select_for_update().get(pk=job_id)
        _require_confirmation(job, client_id=client_id, revision=revision, confirmed=confirmed)
        if job.status != JobStatus.FAILED:
            raise OperationConflict("Only failed jobs can be retried.")
        job.status = JobStatus.QUEUED
        job.attempt = 0
        job.run_after = timezone.now()
        job.task_result_id = ""
        job.error = ""
        job.response_status = None
        job.response_body = ""
        job.lease_expires_at = None
        job.started_at = None
        job.finished_at = None
        job.save()
        # A transport outage leaves a recoverable queued record; the scheduler's
        # recover_unenqueued_jobs sweep retries dispatch instead of reporting
        # an HTTP failure after this state change already committed.
        transaction.on_commit(lambda: enqueue_job(job.pk), robust=True)
    return job


def change_schedule_state(schedule_id, action, *, client_id, revision, confirmed):
    """Pause/resume future submissions without performing queued work."""
    with transaction.atomic():
        schedule = Schedule.objects.select_for_update().get(pk=schedule_id)
        _require_confirmation(schedule, client_id=client_id, revision=revision, confirmed=confirmed)
        if action not in {"pause", "resume"}:
            raise OperationConflict("Choose pause or resume.")
        enabled = action == "resume"
        if schedule.enabled != enabled:
            schedule.enabled = enabled
            if enabled:
                try:
                    schedule.next_run_at = next_occurrence(
                        schedule.cron, timezone_name=schedule.timezone
                    )
                except (ValueError, OverflowError) as exc:
                    raise OperationConflict("This schedule has an invalid run policy. Correct its cron definition through the schedules API before resuming.") from exc
            schedule.save(update_fields=["enabled", "next_run_at", "updated_at"])
    return schedule
