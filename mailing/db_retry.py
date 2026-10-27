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
import os
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


def _in_atomic_block() -> bool:
    return any(conn.in_atomic_block for conn in connections.all(initialized_only=True))


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

    Inside an enclosing ``atomic`` block nothing is retried: the outer
    transaction is already aborted (and closing its connection would poison
    it), so the error goes to whoever owns that transaction.
    """
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except (DatabaseError, InterfaceError) as exc:
            if not is_transient_db_error(exc) or attempt == attempts or _in_atomic_block():
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


# --- Long-running loops ------------------------------------------------------
#
# The 2026-10-22/23 incident: Coolify recreated the Postgres container with a
# new IP, and the long-lived workers kept their persistent connections
# (CONN_MAX_AGE=600) to the old one. Request/response code gets
# ``close_old_connections`` for free from Django's request signals; a bare
# ``while True`` loop never does, so a dead socket was never dropped and DNS was
# never re-resolved. Every loop iteration now drops connections that errored or
# aged out; with ``CONN_HEALTH_CHECKS`` the next query also pings a reused
# connection first, and a reconnect resolves the host name afresh.

LOOP_RETRY_ATTEMPTS = 3
LOOP_RETRY_BASE_SECONDS = 1.0
LOOP_RETRY_MAX_SECONDS = 8.0
LOOP_OUTAGE_SLEEP_SECONDS = 8.0

_loop_started = False


def drop_unusable_connections() -> None:
    """``close_old_connections`` for loops: drop broken or expired connections.

    Skips a connection inside an atomic block (only a test wraps a loop in
    one; closing there would poison the surrounding transaction).
    """
    for conn in connections.all(initialized_only=True):
        if not conn.in_atomic_block:
            conn.close_if_unusable_or_obsolete()


def record_db_heartbeat() -> None:
    """Touch the heartbeat file after an iteration that reached the database.

    ``worker_healthcheck`` (the container healthcheck, run inside the same
    container) fails when this file goes stale, so a worker whose own
    connection is stuck turns the container unhealthy even though a fresh
    ``docker exec`` process can still reach Postgres.
    """
    from django.conf import settings  # noqa: PLC0415 - keep this module importable without settings

    path = getattr(settings, "RELAY_DB_HEARTBEAT_FILE", "")
    if not path:
        return
    try:
        with open(path, "a"):
            pass
        os.utime(path, None)
    except OSError as exc:
        logger.warning("could not write DB heartbeat file %s: %s", path, exc)


def first_line(exc: BaseException) -> str:
    """The first line of a DB error (psycopg2 appends DETAIL/HINT lines)."""
    lines = str(exc).strip().splitlines()
    return lines[0] if lines else exc.__class__.__name__


def run_loop_iteration(func: Callable[[], T], *, action: str, once: bool = False) -> tuple[bool, T | None]:
    """Run one iteration of a long-running loop with DB self-healing.

    Drops unusable connections first, retries transient DB errors with
    backoff, and if the outage outlasts the retries waits it out instead of
    letting the loop die (``once`` mode re-raises: there is no next
    iteration). Returns ``(True, result)`` after a successful iteration and
    ``(False, None)`` after a waited-out outage. Non-transient errors raise.
    """
    global _loop_started
    if not _loop_started:
        # Mark the process start, so a loop stuck in its very first
        # iteration also goes stale (a missing file only skips the check).
        _loop_started = True
        record_db_heartbeat()
    drop_unusable_connections()
    try:
        result = run_with_db_retry(
            func,
            action=action,
            attempts=LOOP_RETRY_ATTEMPTS,
            base_delay_seconds=LOOP_RETRY_BASE_SECONDS,
            max_delay_seconds=LOOP_RETRY_MAX_SECONDS,
        )
    except (DatabaseError, InterfaceError) as exc:
        if once or not is_transient_db_error(exc):
            raise
        logger.warning(
            "%s: retrying transient DB error after an outage pause of %.0fs: %s",
            action,
            LOOP_OUTAGE_SLEEP_SECONDS,
            first_line(exc),
        )
        connections.close_all()
        sleep(LOOP_OUTAGE_SLEEP_SECONDS)
        return False, None
    record_db_heartbeat()
    return True, result
