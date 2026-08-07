"""Progress reporting for the running Relay task.

Two shapes are supported deliberately, and a third is deliberately not.

Countable fan-out: the parent knows the total before enqueuing children, calls
``set_total(n)``, and the numerator is derived from finished children. Nothing
is required of the children.

Countable loop: a task iterating rows in-process calls ``set_total(n)`` once
and ``report(i)`` as it goes.

Opaque work: no progress at all. Leave both columns null; the console renders
elapsed time against a rolling baseline instead of a meaningless empty bar.

Deferred outcomes such as email opens are not progress and must not be routed
here — they arrive over days, never terminate, and would make every task look
like it is still running. They belong on the domain object.
"""

import logging

from django.utils import timezone

from taskdeck import context
from taskdeck.models import TaskRun

logger = logging.getLogger("taskdeck")


def _current_qs():
    run_id = context.current_run_id()
    if run_id is None:
        return None
    return TaskRun.objects.filter(pk=run_id)


def set_total(total, message=""):
    """Declare the denominator for the running task."""
    qs = _current_qs()
    if qs is None:
        logger.debug("taskdeck.set_total called outside a task; ignoring")
        return False
    fields = {"progress_total": int(total), "heartbeat_at": timezone.now()}
    if message:
        fields["message"] = message[:500]
    return bool(qs.update(**fields))


def report(current, message="", total=None):
    """Update the numerator for an in-process loop."""
    qs = _current_qs()
    if qs is None:
        logger.debug("taskdeck.report called outside a task; ignoring")
        return False
    fields = {"progress_current": int(current), "heartbeat_at": timezone.now()}
    if total is not None:
        fields["progress_total"] = int(total)
    if message:
        fields["message"] = message[:500]
    return bool(qs.update(**fields))


def heartbeat(message=""):
    """Signal liveness from a long task that has no countable progress.

    Worth calling inside any loop that can run for minutes. Without it a wedged
    task and a slow task look identical, which is the failure the console is
    supposed to catch.
    """
    qs = _current_qs()
    if qs is None:
        return False
    fields = {"heartbeat_at": timezone.now()}
    if message:
        fields["message"] = message[:500]
    return bool(qs.update(**fields))


def set_entity(entity_type, entity_id):
    """Record which domain object this run is about, for console links."""
    qs = _current_qs()
    if qs is None:
        return False
    return bool(qs.update(entity_type=str(entity_type)[:64], entity_id=str(entity_id)[:255]))


def stamp(task_result, *, entity=None, owner_id=None, message=None):
    """Annotate the run for a task that was just enqueued.

    ``set_entity``/``set_owner`` act on the *currently executing* task, so a
    task that is only queued carries nothing until a worker picks it up. That
    leaves a console showing a queue of anonymous rows, which is when context
    is most wanted. This stamps the row at enqueue time instead.

    ``task_result`` is whatever ``.enqueue()`` returned; a bare id also works.
    """
    result_id = str(getattr(task_result, "id", task_result) or "")
    if not result_id:
        return False

    fields = {}
    if entity is not None:
        entity_type, entity_id = entity
        fields["entity_type"] = str(entity_type)[:64]
        fields["entity_id"] = str(entity_id)[:255]
    if owner_id is not None:
        fields["owner_id"] = str(owner_id)[:255]
    if message is not None:
        fields["message"] = str(message)[:500]
    if not fields:
        return False

    return bool(TaskRun.objects.filter(result_id=result_id).update(**fields))


def set_owner(owner_id):
    """Attach the tenant this run belongs to.

    Set as early in the task as possible: rows written before this call carry
    an empty owner and will not appear in a client-scoped view.
    """
    qs = _current_qs()
    if qs is None:
        return False
    return bool(qs.update(owner_id=str(owner_id)[:255]))
