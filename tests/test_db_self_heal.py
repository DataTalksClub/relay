"""Long-running loops must self-heal a dead database connection.

2026-10-22/23: Coolify recreated the Postgres container with a new IP and the
long-lived workers kept persistent connections to the old IP until someone
restarted them. Request code gets ``close_old_connections`` from Django's
request signals; ``while True`` loops did not. Each loop iteration now drops
unusable connections (and ``CONN_HEALTH_CHECKS`` pings a reused one), retries
transient errors, and waits out longer outages instead of dying.

The tests marked PostgreSQL kill the worker's real backend with
``pg_terminate_backend`` from a second connection -- the closest local
equivalent of the server going away under a persistent connection.
"""

import hashlib
import inspect
import os
import time
from unittest.mock import patch

import psycopg2
import pytest
from django.core.management import CommandError, call_command
from django.db import OperationalError, connection, connections
from django.tasks import TaskResultStatus, task
from django_tasks_db.management.commands import db_worker as upstream
from django_tasks_db.models import DBTaskResult

from mailing import db_retry
from relay.management.commands.db_worker import ResilientWorker

pytestmark = pytest.mark.django_db(transaction=True)

postgres_only = pytest.mark.skipif(connection.vendor != "postgresql", reason="needs a real PostgreSQL backend")


@task()
def heal_probe():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return "ok"


class _StopLoop(Exception):
    pass


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(db_retry, "sleep", recorded.append)
    return recorded


def _backend_pid():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def _terminate_backend(pid):
    """Kill ``pid`` from an independent connection, as a Postgres restart would."""
    params = connection.get_connection_params()
    params.pop("cursor_factory", None)
    params.pop("context", None)
    killer = psycopg2.connect(**params)
    try:
        killer.autocommit = True
        with killer.cursor() as cursor:
            cursor.execute("SELECT pg_terminate_backend(%s)", [pid])
    finally:
        killer.close()
    time.sleep(0.2)


# --- dispatcher loops -------------------------------------------------------


def test_dispatcher_survives_a_dropped_connection_and_continues(sleeps):
    results = [
        OperationalError("server closed the connection unexpectedly"),
        {"processed": 1, "delivered": 1, "failed": 0},
        _StopLoop(),
    ]
    with (
        patch(
            "mailing.management.commands.process_client_callbacks.process_due_client_callbacks",
            side_effect=results,
        ) as process,
        patch.object(connections, "close_all") as close_all,
    ):
        with pytest.raises(_StopLoop):
            call_command("process_client_callbacks", "--idle-sleep", "0")

    assert process.call_count == 3
    assert close_all.called  # the dead socket was dropped before the retry
    assert sleeps == [1.0]


def test_dispatcher_waits_out_a_longer_outage_instead_of_exiting(sleeps, settings):
    outage = OperationalError('could not translate host name "mqimefrti7ratmaliirnikov" to address')
    with patch(
        "mailing.management.commands.process_recipient_list_imports.process_pending_recipient_list_import_jobs",
        side_effect=[outage, outage, outage, {"processed": 0, "succeeded": 0, "failed": 0}, _StopLoop()],
    ):
        with pytest.raises(_StopLoop):
            call_command("process_recipient_list_imports", "--idle-sleep", "0")

    assert sleeps == [1.0, 2.0, 8.0]
    assert os.path.exists(settings.RELAY_DB_HEARTBEAT_FILE)


def test_once_mode_still_fails_loudly(sleeps):
    with patch(
        "mailing.management.commands.process_cmp_callbacks.process_due_cmp_callbacks",
        side_effect=OperationalError("connection refused"),
    ):
        with pytest.raises(OperationalError, match="connection refused"):
            call_command("process_cmp_callbacks", "--once")


@postgres_only
def test_dispatcher_reconnects_after_postgres_kills_its_connection():
    pids = []

    def process_due_client_callbacks(limit):
        pids.append(_backend_pid())
        if len(pids) == 1:
            _terminate_backend(pids[0])
        if len(pids) == 3:
            raise _StopLoop()
        return {"processed": 1, "delivered": 0, "failed": 0}

    with patch(
        "mailing.management.commands.process_client_callbacks.process_due_client_callbacks",
        process_due_client_callbacks,
    ):
        with pytest.raises(_StopLoop):
            call_command("process_client_callbacks")

    assert len(pids) == 3
    assert pids[1] != pids[0]  # fresh connection after the kill
    assert pids[2] == pids[1]  # and then reused again


# --- db_worker ---------------------------------------------------------------


def test_db_worker_override_is_the_installed_command():
    from django.core.management import get_commands  # noqa: PLC0415

    assert get_commands()["db_worker"] == "relay"


def test_db_worker_loop_mirrors_the_pinned_upstream_version():
    """ResilientWorker.run copies django_tasks_db 0.12's loop; re-sync on upgrade."""
    digest = hashlib.sha256(inspect.getsource(upstream.Worker.run).encode()).hexdigest()
    assert digest == "13ac44f99fbdb6e98a0c791d67c286ce916788c572448161a626ba2b8bb5f661"
    handle = hashlib.sha256(inspect.getsource(upstream.Command.handle).encode()).hexdigest()
    assert handle == "c00f44398a1bc6ceab9f9a5490c193ea40bd2e5d14593d21795cb1d8cf6df2b0"


def _worker(**overrides):
    kwargs = {
        "queue_names": ["default"],
        "interval": 0,
        "batch": False,
        "backend_name": "default",
        "startup_delay": False,
        "max_tasks": 1,
        "worker_id": "self-heal-test",
    }
    return ResilientWorker(**(kwargs | overrides))


def test_db_worker_survives_a_transient_error_while_polling(sleeps, settings):
    result = heal_probe.enqueue()
    worker = _worker()
    real_claim = worker._claim_next_task
    calls = []

    def claim():
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("server closed the connection unexpectedly")
        return real_claim()

    worker._claim_next_task = claim
    with patch.object(connections, "close_all"):
        worker.run()

    assert len(calls) == 2
    assert sleeps == [1.0]
    assert DBTaskResult.objects.get(pk=result.id).status == TaskResultStatus.SUCCESSFUL
    assert os.path.exists(settings.RELAY_DB_HEARTBEAT_FILE)


def test_db_worker_batch_mode_fails_loudly_on_an_outage(sleeps):
    worker = _worker(batch=True)
    worker._claim_next_task = lambda: (_ for _ in ()).throw(OperationalError("connection refused"))
    with patch.object(connections, "close_all"), pytest.raises(OperationalError):
        worker.run()


@postgres_only
def test_db_worker_reconnects_after_postgres_kills_its_connection():
    result = heal_probe.enqueue()
    stale_pid = _backend_pid()
    _terminate_backend(stale_pid)

    _worker().run()

    assert DBTaskResult.objects.get(pk=result.id).status == TaskResultStatus.SUCCESSFUL
    assert _backend_pid() != stale_pid


# --- worker_healthcheck --------------------------------------------------------


def test_healthcheck_passes_with_a_db_round_trip_and_fresh_heartbeat(settings, capsys):
    db_retry.record_db_heartbeat()
    call_command("worker_healthcheck", "--queue-name", "default")
    out = capsys.readouterr().out
    assert "worker_healthcheck: alive scope=default" in out
    assert "heartbeat_age=0s" in out


def test_healthcheck_without_a_heartbeat_file_relies_on_the_round_trip(capsys):
    call_command("worker_healthcheck")
    assert "heartbeat_age=none" in capsys.readouterr().out


def test_healthcheck_fails_when_the_worker_heartbeat_is_stale(settings):
    db_retry.record_db_heartbeat()
    old = time.time() - 1000
    os.utime(settings.RELAY_DB_HEARTBEAT_FILE, (old, old))

    with pytest.raises(CommandError, match="heartbeat stale"):
        call_command("worker_healthcheck")
    call_command("worker_healthcheck", "--max-heartbeat-age", "0")


def test_healthcheck_fails_when_the_database_is_unreachable():
    with patch.object(connection, "cursor", side_effect=OperationalError("connection refused")):
        with pytest.raises(CommandError, match="database unreachable: OperationalError"):
            call_command("worker_healthcheck")


@postgres_only
def test_healthcheck_bounds_the_round_trip_on_postgres(capsys):
    call_command("worker_healthcheck", "--timeout", "3")
    assert "worker_healthcheck: alive" in capsys.readouterr().out
    assert connection.settings_dict["OPTIONS"]["connect_timeout"] == 3
    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        assert cursor.fetchone()[0] == "0"  # SET LOCAL did not leak
