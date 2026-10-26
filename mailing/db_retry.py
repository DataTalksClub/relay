"""Retry helper for transient Postgres failures.

On 2026-10-26 the production ``worker`` container logged a single
``OperationalError: deadlock detected`` while two concurrent sends touched
the same contact row, and the host watchdog (which restarts any container
whose logs show an ``OperationalError`` after startup) restarted an
otherwise healthy worker. DNS, the ``coolify`` network, and the Postgres
container itself were all healthy at the time.

A single aborted transaction -- a deadlock, a brief DNS miss during a
Coolify network reattach, or a dead pooled connection after a Postgres IP
churn -- must not fail a task permanently nor stop a long-running loop.
The task layer already re-enqueues a delayed attempt, but the row update
after an SES send should succeed on an immediate retry, and loops such as
``run_relay_scheduler`` must survive a short outage instead of exiting.
"""

import logging
from collections.abc import Callable
from time import sleep
from typing import TypeVar

from django.db import DatabaseError, InterfaceError, connections

logger = logging.getLogger(__name__)

TRANSIENT_DB_ERROR_SNIPPETS = (
    # Coolify/Docker embedded-DNS blips while a network reattaches.
    "could not translate host name",
    "temporary failure in name resolution",
    "name or service not known",
    "name resolution timed out",
    # Postgres not yet accepting connections.
    "connection refused",
    "connection timed out",
    "timeout expired",
    "database system is starting up",
    "the database system is starting up",
    # A dead pooled connection after a Postgres container IP churn.
    "server closed the connection unexpectedly",
    "connection already closed",
    "connection is closed",
    "terminating connection",
    "connection reset",
    "ssl connection has been closed unexpectedly",
    "remaining connection slots are reserved",
    # Concurrent writers; Postgres aborts exactly one of them.
    "deadlock detected",
    "could not serialize access",
)


def is_transient_db_error(exc: BaseException) -> bool:
    """True when ``exc`` is a database error worth retrying with backoff.

    Covers both ``DatabaseError`` (``OperationalError`` et al) and
    ``InterfaceError``: Django models the latter as a sibling of the
    former, while the host watchdog treats either string in the logs as a
    sick worker.
    """
    if not isinstance(exc, (DatabaseError, InterfaceError)):
        return False
    message = str(exc).lower()
    return any(snippet in message for snippet in TRANSIENT_DB_ERROR_SNIPPETS)


T = TypeVar("T")


def run_with_db_retry(
    func: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay_seconds: float = 0.5,
    max_delay_seconds: float = 5.0,
    action: str = "database operation",
) -> T:
    """Run ``func``, retrying transient DB errors with exponential backoff.

    Only :class:`~django.db.DatabaseError` / :class:`~django.db.InterfaceError`
    whose message matches :data:`TRANSIENT_DB_ERROR_SNIPPETS` is retried; anything else (a missing
    table, a constraint violation, a programming error) raises immediately.
    Stale pooled connections are dropped before each retry so the next
    attempt reconnects instead of reusing the broken socket.
    """
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except (DatabaseError, InterfaceError) as exc:
            if not is_transient_db_error(exc) or attempt == attempts:
                raise
            delay = min(base_delay_seconds * 2 ** (attempt - 1), max_delay_seconds)
            logger.warning(
                "%s hit a transient DB error (attempt %d/%d); retrying in %.1fs: %s",
                action,
                attempt,
                attempts,
                delay,
                exc,
            )
            connections.close_all()
            sleep(delay)
    raise AssertionError("unreachable: retry loop always returns or raises")
