import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.management.base import CommandError
from django.db import OperationalError

from mailing.management.commands.migrate_before_start import (
    MIGRATION_LOCK_ID,
    Command,
    _acquire_migration_lock_with_retry,
    _apply_migrations_with_retry,
)


def _pending_executor():
    executor = MagicMock()
    executor.loader.graph.leaf_nodes.return_value = [("mailing", "0001_initial")]
    executor.migration_plan.return_value = [("mailing", "0001_initial")]
    return executor


@pytest.mark.parametrize("fails", [False, True])
def test_postgresql_gate_serializes_migrations_and_releases_lock(fails):
    connection = MagicMock(vendor="postgresql")
    cursor = connection.cursor.return_value.__enter__.return_value
    events = []
    cursor.execute.side_effect = lambda sql, params=None: events.append((sql, params))
    executor = _pending_executor()

    def migrate(*args, **kwargs):
        events.append((args, kwargs))
        if fails:
            raise CommandError("migration failed")

    with (
        patch("mailing.management.commands.migrate_before_start.connections", {"default": connection}),
        patch("mailing.management.commands.migrate_before_start.MigrationExecutor", return_value=executor),
        patch("mailing.management.commands.migrate_before_start.call_command", side_effect=migrate),
    ):
        if fails:
            with pytest.raises(CommandError, match="migration failed"):
                Command().handle(verbosity=1)
        else:
            Command().handle(verbosity=1)

    assert events == [
        ("SET lock_timeout = '5s'", None),
        ("SELECT pg_advisory_lock(%s)", [MIGRATION_LOCK_ID]),
        ("SET lock_timeout = '5s'", None),
        (("migrate",), {"interactive": False, "verbosity": 1}),
        ("SELECT pg_advisory_unlock(%s)", [MIGRATION_LOCK_ID]),
    ]


def test_sqlite_gate_runs_migrations_without_postgresql_lock():
    connection = MagicMock(vendor="sqlite")
    with (
        patch("mailing.management.commands.migrate_before_start.connections", {"default": connection}),
        patch("mailing.management.commands.migrate_before_start.call_command") as migrate,
    ):
        Command().handle(verbosity=0)
    migrate.assert_called_once_with("migrate", interactive=False, verbosity=0)
    connection.cursor.assert_not_called()


def test_postgresql_gate_skips_lock_when_schema_is_current():
    connection = MagicMock(vendor="postgresql")
    executor = _pending_executor()
    executor.migration_plan.return_value = []
    out = MagicMock()
    with (
        patch("mailing.management.commands.migrate_before_start.connections", {"default": connection}),
        patch("mailing.management.commands.migrate_before_start.MigrationExecutor", return_value=executor),
        patch("mailing.management.commands.migrate_before_start.call_command") as migrate,
    ):
        Command(stdout=out).handle(verbosity=0)
    connection.cursor.assert_not_called()
    migrate.assert_not_called()
    out.write.assert_called_once_with("No unapplied migrations; startup migration gate skipped.\n")


def test_migration_lock_timeout_retries_acquisition():
    connection = MagicMock(vendor="postgresql")
    cursor = connection.cursor.return_value.__enter__.return_value
    advisory_attempts = 0

    def execute(sql, params=None):
        nonlocal advisory_attempts
        if sql == "SELECT pg_advisory_lock(%s)":
            advisory_attempts += 1
            if advisory_attempts == 1:
                raise OperationalError("canceling statement due to lock timeout")

    cursor.execute.side_effect = execute
    out = MagicMock()
    with patch("mailing.management.commands.migrate_before_start.time.sleep") as sleep:
        _acquire_migration_lock_with_retry(connection, out)

    assert advisory_attempts == 2
    assert sleep.call_count == 1
    assert "SET lock_timeout = '5s'" in [call.args[0] for call in cursor.execute.call_args_list]


def test_migration_statement_lock_timeout_retries_migration():
    connection = MagicMock(vendor="postgresql")
    out = MagicMock()
    with (
        patch(
            "mailing.management.commands.migrate_before_start.call_command",
            side_effect=[OperationalError("canceling statement due to lock timeout"), None],
        ) as migrate,
        patch("mailing.management.commands.migrate_before_start.time.sleep") as sleep,
    ):
        _apply_migrations_with_retry(connection, out, verbosity=0)

    assert migrate.call_count == 2
    sleep.assert_called_once_with(2)


def test_startup_waits_for_docker_dns_before_any_migration_or_lock():
    connection = MagicMock(vendor="postgresql")
    connection.ensure_connection.side_effect = [
        OperationalError('could not translate host name "private-host" to address: Temporary failure in name resolution'),
        OperationalError("connection refused"),
        None,
    ]
    with (
        patch("mailing.management.commands.migrate_before_start.connections", {"default": connection}),
        patch("mailing.management.commands.migrate_before_start.MigrationExecutor", return_value=_pending_executor()),
        patch("mailing.management.commands.migrate_before_start.time.sleep") as sleep,
        patch("mailing.management.commands.migrate_before_start.call_command") as migrate,
    ):
        Command().handle(verbosity=0)
    assert connection.ensure_connection.call_count == 3
    assert connection.close.call_count == 2
    assert sleep.call_count == 2
    migrate.assert_called_once_with("migrate", interactive=False, verbosity=0)


@pytest.mark.parametrize("message,expected_attempts", [
    ("Temporary failure in name resolution", 30),
    ("password authentication failed", 1),
])
def test_connection_wait_is_bounded_and_never_retries_auth_or_runs_migrations(message, expected_attempts):
    connection = MagicMock(vendor="postgresql")
    connection.ensure_connection.side_effect = OperationalError(message)
    with (
        patch("mailing.management.commands.migrate_before_start.connections", {"default": connection}),
        patch("mailing.management.commands.migrate_before_start.time.sleep") as sleep,
        patch("mailing.management.commands.migrate_before_start.call_command") as migrate,
        pytest.raises((CommandError, OperationalError)),
    ):
        Command().handle(verbosity=0)
    assert connection.ensure_connection.call_count == expected_attempts
    assert sleep.call_count == expected_attempts - 1
    connection.cursor.assert_not_called()
    migrate.assert_not_called()


@pytest.mark.parametrize("migration_status", [0, 1])
def test_image_entrypoint_only_launches_application_after_success(tmp_path, migration_status):
    python = tmp_path / "python"
    python.write_text(
        '#!/bin/sh\n'
        'test "$1" = /app/manage.py && test "$2" = migrate_before_start || exit 99\n'
        'echo migration >> "$GATE_EVENTS"\n'
        f"exit {migration_status}\n"
    )
    python.chmod(0o755)
    events = tmp_path / "events"
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["/bin/sh", str(root / "scripts/container-entrypoint.sh"), "/bin/sh", "-c", 'echo application >> "$GATE_EVENTS"'],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "GATE_EVENTS": str(events)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == migration_status
    assert events.read_text().splitlines() == (["migration", "application"] if migration_status == 0 else ["migration"])
    assert 'ENTRYPOINT ["/bin/sh", "/app/scripts/container-entrypoint.sh"]' in (root / "Dockerfile").read_text()
