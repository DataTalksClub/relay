from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management.base import CommandError

from mailing.management.commands.db_preflight import Command
from mailing.management.commands.migrate_before_start import MIGRATION_LOCK_ID


def fake_connection(objects=(), identity=("relay_app", "relay_app", "relay")):
    connection = MagicMock(vendor="postgresql")
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = identity
    cursor.fetchall.return_value = objects
    return connection, cursor


@pytest.mark.parametrize("kind,name", [
    ("r", "public.transactional_messages"), ("p", "public.events"),
    ("S", "public.clients_id_seq"), ("v", "public.client_view"),
    ("m", "public.materialized"), ("i", "public.client_idx"),
    ("I", "public.partition_idx"), ("f", "public.layout(integer)"),
])
def test_ownership_failure_names_every_object_and_never_migrates(kind, name):
    connection, cursor = fake_connection([(kind, name, "pgadmin")])
    out, err = StringIO(), StringIO()
    with (
        patch("mailing.management.commands.db_preflight.connections", {"default": connection}),
        patch("mailing.management.commands.db_preflight.call_command") as migrate,
        pytest.raises(CommandError, match="not owned by migration role relay_app"),
    ):
        Command(stdout=out, stderr=err).handle(expected_role="relay_app", expected_database="relay")
    migrate.assert_not_called()
    assert f"{name} kind={kind} owner=pgadmin; expected=relay_app" in err.getvalue()
    assert cursor.execute.call_args.args == ("SELECT pg_advisory_unlock(%s)", [MIGRATION_LOCK_ID])


@pytest.mark.parametrize("identity", [
    ("pgadmin", "pgadmin", "relay"), ("relay_app", "pgadmin", "relay"),
    ("relay_app", "relay_app", "relay_dev"),
])
def test_wrong_database_or_admin_identity_fails_before_plan(identity):
    connection, _ = fake_connection(identity=identity)
    with (
        patch("mailing.management.commands.db_preflight.connections", {"default": connection}),
        patch("mailing.management.commands.db_preflight.call_command") as migrate,
        pytest.raises(CommandError, match="identity mismatch"),
    ):
        Command().handle(expected_role="relay_app", expected_database="relay")
    migrate.assert_not_called()


def test_success_checks_ownership_before_and_after_plan_apply_and_check():
    connection, cursor = fake_connection([("r", "public.clients", "relay_app")])
    out = StringIO()
    with (
        patch("mailing.management.commands.db_preflight.connections", {"default": connection}),
        patch("mailing.management.commands.db_preflight.call_command") as migrate,
    ):
        Command(stdout=out).handle(expected_role="relay_app", expected_database="relay")
    assert [c.kwargs for c in migrate.call_args_list] == [
        dict(plan=True, interactive=False, stdout=migrate.call_args_list[0].kwargs["stdout"], verbosity=1),
        dict(interactive=False, stdout=migrate.call_args_list[0].kwargs["stdout"], verbosity=1),
        dict(check_unapplied=True, interactive=False, stdout=migrate.call_args_list[0].kwargs["stdout"], verbosity=0),
    ]
    assert cursor.fetchall.call_count == 2
    assert "DB preflight PASS" in out.getvalue()


def test_migration_failure_releases_lock_and_sanitizes_exception():
    connection, cursor = fake_connection()
    with (
        patch("mailing.management.commands.db_preflight.connections", {"default": connection}),
        patch("mailing.management.commands.db_preflight.call_command", side_effect=ValueError("private-value")),
        pytest.raises(CommandError, match="do not replace serving containers") as error,
    ):
        Command().handle(expected_role="relay_app", expected_database="relay")
    assert "private-value" not in str(error.value)
    assert "pg_advisory_unlock" in cursor.execute.call_args.args[0]


def test_sqlite_cannot_silently_skip_deploy_ownership_check():
    with (
        patch("mailing.management.commands.db_preflight.connections", {"default": MagicMock(vendor="sqlite")}),
        pytest.raises(CommandError, match="requires PostgreSQL"),
    ):
        Command().handle(expected_role="relay_app", expected_database="relay")
