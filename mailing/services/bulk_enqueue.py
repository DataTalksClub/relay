"""Batch insertion of existing send tasks; all preparation stays synchronous."""
import uuid

from django.conf import settings
from django.db import transaction
from django_tasks_db.backend import DatabaseBackend
from django_tasks_db.models import DBTaskResult, get_date_max

from taskdeck import context
from taskdeck.models import TaskRun


def enqueue_send_tasks(task, payloads, *, entity_type, owner_id="", correlation_id=None):
    backend = task.get_backend()
    if not isinstance(backend, DatabaseBackend):
        return False
    backend.validate_task(task)
    rows = [DBTaskResult(
        id=backend.id_function(),
        args_kwargs={"args": [payload], "kwargs": {}}, priority=task.priority,
        task_path=task.module_path, queue_name=task.queue_name,
        backend_name=backend.alias,
        run_after=task.run_after or get_date_max(),
    ) for payload in payloads]
    with transaction.atomic():
        DBTaskResult.objects.bulk_create(rows, batch_size=500)
        TaskRun.objects.bulk_create([
            TaskRun(
                result_id=str(row.pk), project=getattr(settings, "TASKDECK_PROJECT", ""),
                name=task.name, func=task.module_path,
                correlation_id=correlation_id or context.current_correlation_id() or uuid.uuid4(),
                parent_id=context.current_run_id(),
                owner_id=str(owner_id or payload.get("client_id") or context.current_owner_id() or ""),
                entity_type=entity_type,
                entity_id=str(payload.get("campaign_id") or payload.get("transactional_message_id") or ""),
            ) for row, payload in zip(rows, payloads, strict=True)
        ], batch_size=500)
    return True
