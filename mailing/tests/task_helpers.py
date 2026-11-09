"""Run queued django-tasks-db rows inline, the way db_worker would, inside a test transaction."""

from django.utils import timezone
from django_tasks_db.models import DBTaskResult


def drain_queue(queue_name):
    ran = 0
    while True:
        result = DBTaskResult.objects.ready().filter(queue_name=queue_name).order_by("enqueued_at", "id").first()
        if result is None:
            return ran
        # RUNNING like db_worker, so "no READY task left" drain checks behave the same.
        DBTaskResult.objects.filter(pk=result.pk).update(status="RUNNING", started_at=timezone.now())
        result.task.call(*result.args_kwargs["args"], **result.args_kwargs["kwargs"])
        DBTaskResult.objects.filter(pk=result.pk).update(status="SUCCESSFUL", finished_at=timezone.now())
        ran += 1
