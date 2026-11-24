"""Per-transaction commit durability (``SET LOCAL synchronous_commit``).

Only ever scoped to the current transaction: roles and the server keep their
configured default. Both helpers are no-ops outside PostgreSQL or outside an
atomic block (``SET LOCAL`` would not outlive the statement there).

Why async commit is crash-safe for the event lanes: they run as django-tasks DB
tasks whose completion is a later, synchronous commit on the same server. WAL is
flushed in order, so once a task is recorded as done its earlier async commit is
durable too; a crash before that leaves the task to run again, and provider events
are idempotent by ``provider_event_id``.
"""

from django.db import connection


def _set_local(value):
    if connection.vendor != "postgresql" or not connection.in_atomic_block:
        return False
    with connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL synchronous_commit = {value}")
    return True


def set_local_async_commit():
    """Skip the commit fsync for this transaction (idempotent, replayable writes only)."""
    return _set_local("off")


def set_local_sync_commit():
    """Force a durable commit for this transaction whatever the session/role default is."""
    return _set_local("on")
