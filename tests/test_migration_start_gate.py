import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.management.base import CommandError
from django.db import OperationalError

from mailing.management.commands.migrate_before_start import MIGRATION_LOCK_ID, Command


@pytest.mark.parametrize("fails", [False, True])
def test_postgresql_gate_serializes_migrations_and_releases_lock(fails):
    connection = MagicMock(vendor="postgresql")
    cursor = connection.cursor.return_value.__enter__.return_value
    events = []
    cursor.execute.side_effect = lambda sql, params: events.append((sql, params))

    def migrate(*args, **kwargs):
        events.append((args, kwargs))
        if fails:
            raise CommandError("migration failed")

    with (
        patch("mailing.management.commands.migrate_before_start.connections", {"default": connection}),
        patch("mailing.management.commands.migrate_before_start.call_command", side_effect=migrate),
    ):
        if fails:
            with pytest.raises(CommandError, match="migration failed"):
                Command().handle(verbosity=1)
        else:
            Command().handle(verbosity=1)

    assert events == [
        ("SELECT pg_advisory_lock(%s)", [MIGRATION_LOCK_ID]),
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


def test_startup_waits_for_docker_dns_before_any_migration_or_lock():
    connection = MagicMock(vendor="postgresql")
    connection.ensure_connection.side_effect = [
        OperationalError('could not translate host name "private-host" to address: Temporary failure in name resolution'),
        OperationalError("connection refused"),
        None,
    ]
    with (
        patch("mailing.management.commands.migrate_before_start.connections", {"default": connection}),
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
