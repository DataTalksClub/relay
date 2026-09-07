import uuid

from django.db import models
from django.utils import timezone


class JobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    RETRYING = "retrying", "Retrying"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


TERMINAL_JOB_STATUSES = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
)


class Schedule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        "mailing.Client",
        on_delete=models.CASCADE,
        related_name="relay_schedules",
    )
    name = models.SlugField(max_length=120)
    cron = models.CharField(max_length=120)
    task_type = models.CharField(max_length=64)
    task = models.JSONField(default=dict)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    enabled = models.BooleanField(default=True)
    next_run_at = models.DateTimeField(db_index=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_missed_at = models.DateTimeField(null=True, blank=True)
    last_job = models.ForeignKey(
        "Job",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="last_for_schedules",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "relay_schedules"
        ordering = ["client_id", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "name"],
                name="relay_schedule_client_name_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=["enabled", "next_run_at"],
                name="relay_sched_due_idx",
            )
        ]

    def __str__(self):
        return f"{self.client.slug}/{self.name}"


class Job(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        "mailing.Client",
        on_delete=models.PROTECT,
        related_name="relay_jobs",
    )
    schedule = models.ForeignKey(
        Schedule,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="jobs",
    )
    task_type = models.CharField(max_length=64, db_index=True)
    idempotency_key = models.CharField(max_length=255)
    request_hash = models.CharField(max_length=64)
    correlation_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    task = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16,
        choices=JobStatus.choices,
        default=JobStatus.QUEUED,
        db_index=True,
    )
    attempt = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    run_after = models.DateTimeField(default=timezone.now, db_index=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    task_result_id = models.CharField(max_length=255, blank=True)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "relay_jobs"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "idempotency_key"],
                name="relay_job_client_idempotency_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=["client", "status", "-created_at"],
                name="relay_job_cli_status_idx",
            ),
            models.Index(
                fields=["status", "run_after"],
                name="relay_job_due_idx",
            ),
        ]

    @property
    def is_terminal(self):
        return self.status in TERMINAL_JOB_STATUSES

    def __str__(self):
        return f"{self.client.slug}/{self.task_type}/{self.id}"
