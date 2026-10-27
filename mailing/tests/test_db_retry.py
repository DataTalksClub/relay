"""Transient DB errors must retry with backoff; real errors must fail fast.

On 2026-10-26 a single ``deadlock detected`` OperationalError restarted an
otherwise healthy production worker, because any OperationalError in the
logs reads as a sick worker. The helpers here are what keep one aborted
transaction (or a seconds-long DNS miss) from failing a task or stopping
a loop.
"""

import logging
from types import SimpleNamespace

import pytest
from django.db import DatabaseError, InterfaceError, OperationalError

from mailing import db_retry
from mailing.db_retry import deadlock_diag_lines, is_transient_db_error, run_with_db_retry


@pytest.mark.parametrize(
    "message",
    [
        'could not translate host name "mqimefrti7ratmaliirnikov" to address: Temporary failure in name resolution',
        'could not translate host name "db" to address: Name or service not known',
        "connection refused",
        "connection timed out",
        "timeout expired",
        "the database system is starting up",
        "server closed the connection unexpectedly",
        "terminating connection due to administrator command",
        "remaining connection slots are reserved for roles with privileges",
        "SSL connection has been closed unexpectedly",
        "deadlock detected",
        "could not serialize access due to concurrent update",
    ],
)
def test_transient_messages_are_retried(message):
    assert is_transient_db_error(OperationalError(message)) is True
    assert is_transient_db_error(InterfaceError(message)) is True


@pytest.mark.parametrize(
    "exc",
    [
        OperationalError('relation "mailing_client" does not exist'),
        OperationalError('duplicate key value violates unique constraint "x"'),
        OperationalError("syntax error at or near \"FROM\""),
        ValueError("not a database error at all"),
    ],
)
def test_permanent_errors_are_not_retried(exc):
    assert is_transient_db_error(exc) is False


def test_retry_returns_the_first_success(monkeypatch):
    sleeps = []
    monkeypatch.setattr(db_retry, "sleep", sleeps.append)

    assert run_with_db_retry(lambda: "ok") == "ok"
    assert sleeps == []


def test_retry_recovers_after_transient_failures(monkeypatch):
    sleeps = []
    monkeypatch.setattr(db_retry, "sleep", sleeps.append)
    closes = []
    monkeypatch.setattr(db_retry.connections, "close_all", lambda: closes.append(1))

    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise OperationalError("deadlock detected")
        return "recovered"

    assert run_with_db_retry(flaky, base_delay_seconds=0.2, max_delay_seconds=2.0) == "recovered"
    assert len(attempts) == 3
    # Exponential with up to +25% jitter, so the exact sleep varies per run.
    assert 0.2 <= sleeps[0] <= 0.25
    assert 0.4 <= sleeps[1] <= 0.5
    assert len(closes) == 2


def test_retry_gives_up_after_the_last_attempt(monkeypatch):
    sleeps = []
    monkeypatch.setattr(db_retry, "sleep", sleeps.append)

    with pytest.raises(OperationalError, match="could not translate host name"):
        run_with_db_retry(
            lambda: (_ for _ in ()).throw(OperationalError('could not translate host name "db"')),
            attempts=3,
            base_delay_seconds=1.0,
        )
    assert 1.0 <= sleeps[0] <= 1.25
    assert 2.0 <= sleeps[1] <= 2.5


def test_retry_raises_permanent_errors_immediately(monkeypatch):
    sleeps = []
    monkeypatch.setattr(db_retry, "sleep", sleeps.append)
    calls = []

    def broken():
        calls.append(1)
        raise OperationalError('relation "mailing_client" does not exist')

    with pytest.raises(OperationalError, match="does not exist"):
        run_with_db_retry(broken)
    assert calls == [1]
    assert sleeps == []


def _deadlocked(attempt_box):
    """A wrapped deadlock like Django raises it: driver error as ``__cause__``."""
    inner = OperationalError("deadlock detected")
    inner.diag = SimpleNamespace(
        message_primary="deadlock detected",
        message_detail="Process 261 waits for ShareLock on transaction 2558; blocked by process 264.",
        message_hint="See server log for query details.",
    )
    exc = OperationalError("deadlock detected")
    exc.__cause__ = inner
    attempt_box.append(1)
    return exc


def test_deadlock_warning_names_both_parties_from_driver_diag(monkeypatch, caplog):
    monkeypatch.setattr(db_retry, "sleep", lambda seconds: None)
    attempts = []

    def flaky():
        raise _deadlocked(attempts)

    with caplog.at_level(logging.WARNING, logger="mailing.db_retry"):
        with pytest.raises(OperationalError, match="deadlock detected"):
            run_with_db_retry(flaky, attempts=2, base_delay_seconds=0.1)

    deadlock_logs = [record.getMessage() for record in caplog.records if "hit a deadlock" in record.getMessage()]
    assert len(deadlock_logs) == 1
    assert "Process 261 waits for ShareLock on transaction 2558; blocked by process 264." in deadlock_logs[0]
    assert "See server log for query details." in deadlock_logs[0]


def test_deadlock_diag_lines_without_driver_diag_degrade_to_empty():
    exc = OperationalError("deadlock detected")
    assert deadlock_diag_lines(exc) == []
    assert deadlock_diag_lines(ValueError("not a db error")) == []


def test_non_database_errors_are_never_retried(monkeypatch):
    sleeps = []
    monkeypatch.setattr(db_retry, "sleep", sleeps.append)

    with pytest.raises(ValueError, match="boom"):
        run_with_db_retry(lambda: (_ for _ in ()).throw(ValueError("boom")))
    assert sleeps == []


def test_database_error_base_class_counts(monkeypatch):
    monkeypatch.setattr(db_retry, "sleep", lambda seconds: None)
    assert is_transient_db_error(DatabaseError("connection refused")) is True


def test_single_attempt_raises_without_sleeping(monkeypatch):
    sleeps = []
    monkeypatch.setattr(db_retry, "sleep", sleeps.append)

    with pytest.raises(OperationalError, match="deadlock detected"):
        run_with_db_retry(
            lambda: (_ for _ in ()).throw(OperationalError("deadlock detected")),
            attempts=1,
        )
    assert sleeps == []


def test_interface_error_retries_through_the_loop(monkeypatch):
    sleeps = []
    monkeypatch.setattr(db_retry, "sleep", sleeps.append)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise InterfaceError("server closed the connection unexpectedly")
        return "recovered"

    assert run_with_db_retry(flaky, base_delay_seconds=0.5) == "recovered"
    assert len(attempts) == 2
    assert 0.5 <= sleeps[0] <= 0.625


@pytest.mark.django_db
def test_nothing_is_retried_inside_an_enclosing_transaction(monkeypatch):
    """Retrying (and closing the connection) inside an outer atomic block
    would poison that transaction; its owner has to retry instead."""
    from django.db import transaction  # noqa: PLC0415

    sleeps = []
    monkeypatch.setattr(db_retry, "sleep", sleeps.append)
    calls = []

    def deadlock():
        calls.append(1)
        raise OperationalError("deadlock detected")

    with transaction.atomic():
        with pytest.raises(OperationalError, match="deadlock"):
            run_with_db_retry(deadlock, attempts=3)
    assert calls == [1]
    assert sleeps == []
