import uuid

from django.db import models
from django.utils import timezone


class TaskRunStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCESS = "success", "Success"
    FAILED = "failed", "Failed"
    DEAD = "dead", "Dead"


TERMINAL_STATUSES = frozenset({TaskRunStatus.SUCCESS, TaskRunStatus.FAILED, TaskRunStatus.DEAD})


class TaskRun(models.Model):
    """A projection of one task execution, maintained from backend signals.

    This is deliberately not the queue's own record. The backend owns
    scheduling and delivery; this owns the things no backend provides:
    correlation across service boundaries, tenant scoping, progress, and a
    reference back to the domain object the work was about.

    Rows are written by signal receivers (see ``taskdeck.signals``), so a task
    needs no instrumentation to appear here.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # The backend's own identifier for this execution. Kept as text rather than
    # UUID because backends are not required to use UUIDs, and taskdeck must
    # not constrain the backend choice.
    result_id = models.CharField(max_length=255, unique=True)

    project = models.CharField(max_length=64, db_index=True)
    name = models.CharField(max_length=255, db_index=True)
    func = models.CharField(max_length=512, blank=True)

    status = models.CharField(
        max_length=16,
        choices=TaskRunStatus.choices,
        default=TaskRunStatus.QUEUED,
        db_index=True,
    )

    # Tenant scoping. Present from the first migration because backfilling it
    # later means auditing every existing query for the missing filter.
    owner_id = models.CharField(max_length=255, blank=True, db_index=True)

    # Equal to ``id`` for work that started locally; equal to the originator's
    # identifier for work triggered from another service.
    correlation_id = models.UUIDField(db_index=True)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="children",
    )

    progress_current = models.IntegerField(null=True, blank=True)
    progress_total = models.IntegerField(null=True, blank=True)

    enqueued_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)

    attempt = models.IntegerField(default=1)

    message = models.CharField(max_length=500, blank=True)
    error = models.TextField(blank=True)

    entity_type = models.CharField(max_length=64, blank=True)
    entity_id = models.CharField(max_length=255, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["project", "-enqueued_at"], name="td_run_proj_enq_idx"),
            models.Index(fields=["status", "-enqueued_at"], name="td_run_status_enq_idx"),
            models.Index(fields=["name", "-finished_at"], name="td_run_name_fin_idx"),
        ]
        ordering = ["-enqueued_at"]

    def __str__(self):
        return f"{self.project}:{self.name} [{self.status}]"

    @property
    def is_finished(self):
        return self.status in TERMINAL_STATUSES

    @property
    def duration_s(self):
        if self.started_at is None:
            return None
        end = self.finished_at or timezone.now()
        return (end - self.started_at).total_seconds()

    def resolved_progress(self):
        """Return ``(current, total)`` or ``None`` when progress is not tracked.

        A fan-out parent stores only ``progress_total``; the numerator is the
        count of its finished children. Deriving it rather than incrementing a
        column means a child that dies without reporting cannot corrupt the
        parent's count, and no extra write happens per child.
        """
        if self.progress_total is None:
            return None
        if self.progress_current is not None:
            return (self.progress_current, self.progress_total)
        done = self.children.filter(status__in=TERMINAL_STATUSES).count()
        return (done, self.progress_total)
