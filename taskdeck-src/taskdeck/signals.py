"""Bind the TaskRun projection to whichever tasks implementation is in use.

Django 6.0 ships ``django.tasks``. The ``django-tasks`` backport ships
``django_tasks``. They define *separate* ``Signal`` objects, and a backend
emits on whichever one it imported — notably ``django_tasks_db`` emits on the
backport's signals even when the project defines its tasks with Django's
native decorator, because its compat shim aliases the Task class but not the
signals.

Binding to only one of them therefore produces an empty projection and a
console that reports nothing while everything appears to work. We bind to both
and deduplicate on the backend's result id.
"""

import logging
import uuid

from django.db import transaction
from django.utils import timezone

from taskdeck import context
from taskdeck.models import TaskRun, TaskRunStatus

logger = logging.getLogger("taskdeck")

# result_id -> contextvar reset tokens, held between task_started and
# task_finished. Signals fire in the worker's own thread around the task body,
# so a plain dict keyed by result id is sufficient and avoids leaking tokens
# across threads.
_active_tokens = {}


def _signal_modules():
    """Return every signals module present, so we bind to all of them."""
    modules = []
    try:
        from django.tasks import signals as django_signals

        modules.append(django_signals)
    except ImportError:
        pass
    try:
        from django_tasks import signals as backport_signals

        modules.append(backport_signals)
    except ImportError:
        pass
    return modules


def _status_of(task_result):
    raw = getattr(task_result, "status", None)
    value = getattr(raw, "value", raw)
    return str(value or "").upper()


def _task_name(task_result):
    task = getattr(task_result, "task", None)
    name = getattr(task, "name", None)
    if name:
        return str(name)[:255]
    return str(getattr(task, "module_path", "") or "unknown")[:255]


def _module_path(task_result):
    return str(getattr(getattr(task_result, "task", None), "module_path", ""))[:512]


def _error_text(task_result):
    errors = getattr(task_result, "errors", None) or []
    parts = []
    for err in errors:
        traceback = getattr(err, "traceback", None)
        if traceback:
            parts.append(str(traceback))
            continue
        cls = getattr(err, "exception_class_path", None) or getattr(err, "exception_class", None)
        if cls:
            parts.append(str(cls))
    return "\n".join(parts)[:20000]


def _project():
    from django.conf import settings

    return getattr(settings, "TASKDECK_PROJECT", "")[:64]


def on_task_enqueued(sender, task_result, **kwargs):
    """Create the projection row, linking it to the enqueuing task if any."""
    result_id = str(getattr(task_result, "id", "") or "")
    if not result_id:
        return

    parent_id = context.current_run_id()
    correlation_id = context.current_correlation_id()

    try:
        run, created = TaskRun.objects.get_or_create(
            result_id=result_id,
            defaults={
                "project": _project(),
                "name": _task_name(task_result),
                "func": _module_path(task_result),
                "status": TaskRunStatus.QUEUED,
                "owner_id": context.current_owner_id(),
                "parent_id": parent_id,
                # A root task correlates to itself. Assigning the row's own id
                # keeps every chain queryable by a single column regardless of
                # whether it began locally or in another service.
                "correlation_id": correlation_id or uuid.uuid4(),
            },
        )
        if created and correlation_id is None and parent_id is None:
            TaskRun.objects.filter(pk=run.pk).update(correlation_id=run.id)
    except Exception:
        # The projection must never be able to fail an enqueue. A missing row
        # degrades the console; a raised exception loses the user's work.
        logger.exception("taskdeck: failed to record enqueue for %s", result_id)


def on_task_started(sender, task_result, **kwargs):
    result_id = str(getattr(task_result, "id", "") or "")
    if not result_id:
        return
    now = timezone.now()
    try:
        run = TaskRun.objects.filter(result_id=result_id).first()
        if run is None:
            # Enqueue happened in another process that lacked taskdeck, or the
            # row was pruned. Record what we can rather than losing the run.
            run = TaskRun.objects.create(
                result_id=result_id,
                project=_project(),
                name=_task_name(task_result),
                func=_module_path(task_result),
                correlation_id=uuid.uuid4(),
            )
            TaskRun.objects.filter(pk=run.pk).update(correlation_id=run.id)
            run.refresh_from_db()

        TaskRun.objects.filter(pk=run.pk).update(
            status=TaskRunStatus.RUNNING,
            started_at=now,
            heartbeat_at=now,
            attempt=getattr(task_result, "attempts", None) or run.attempt,
        )
        _active_tokens[result_id] = context._set_running(run.id, run.correlation_id, run.owner_id)
    except Exception:
        logger.exception("taskdeck: failed to record start for %s", result_id)


def on_task_finished(sender, task_result, **kwargs):
    result_id = str(getattr(task_result, "id", "") or "")
    if not result_id:
        return

    tokens = _active_tokens.pop(result_id, None)
    try:
        status = _status_of(task_result)
        if status == "SUCCESSFUL":
            mapped = TaskRunStatus.SUCCESS
        elif status == "FAILED":
            mapped = TaskRunStatus.FAILED
        else:
            mapped = TaskRunStatus.SUCCESS

        now = timezone.now()
        TaskRun.objects.filter(result_id=result_id).update(
            status=mapped,
            finished_at=now,
            heartbeat_at=now,
            error=_error_text(task_result),
            attempt=getattr(task_result, "attempts", None) or 1,
        )
    except Exception:
        logger.exception("taskdeck: failed to record finish for %s", result_id)
    finally:
        if tokens is not None:
            try:
                context._reset(tokens)
            except ValueError:
                # Token created in a different context (async backend hopping
                # threads). Nothing to reset; the contextvar dies with it.
                pass


def connect():
    """Wire receivers to every tasks implementation present.

    ``dispatch_uid`` is keyed per module so binding twice is a no-op, and so
    that binding to both implementations does not collide.
    """
    modules = _signal_modules()
    if not modules:
        logger.warning(
            "taskdeck: neither django.tasks nor django_tasks is importable; "
            "the TaskRun projection will stay empty"
        )
        return 0

    for module in modules:
        uid = f"taskdeck:{module.__name__}"
        module.task_enqueued.connect(on_task_enqueued, dispatch_uid=f"{uid}:enqueued", weak=False)
        module.task_started.connect(on_task_started, dispatch_uid=f"{uid}:started", weak=False)
        module.task_finished.connect(on_task_finished, dispatch_uid=f"{uid}:finished", weak=False)
    return len(modules)


def enqueue_on_commit(fn, *args, **kwargs):
    """Enqueue only if the surrounding transaction commits.

    ``django.tasks`` has no on-commit support of its own, so without this every
    caller has to remember ``transaction.on_commit`` and one of them will not.
    Outside a transaction this runs immediately, matching Django's own
    ``on_commit`` semantics.
    """
    transaction.on_commit(lambda: fn(*args, **kwargs))
