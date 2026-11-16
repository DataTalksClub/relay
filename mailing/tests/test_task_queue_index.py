from importlib import import_module
from types import SimpleNamespace

import pytest
from django.db import connection
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

index_migration = import_module("mailing.migrations.0062_provider_queue_ready_index")


@pytest.mark.django_db
def test_queue_index_avoids_unrelated_ready_backlog_and_preserves_order(monkeypatch):
    # The index DDL is also valid SQLite SQL, so exercise it against a real
    # test database; PostgreSQL CI additionally applies the actual migration.
    editor = SimpleNamespace(connection=connection)
    with monkeypatch.context() as patch:
        patch.setattr(connection, "vendor", "postgresql")
        index_migration.create_index(None, editor)
        index_migration.create_index(None, editor)
    DBTaskResult.objects.bulk_create([
        DBTaskResult(task_path="unused", queue_name="provider-events", backend_name="default", args_kwargs={},
                     run_after=timezone.now())
        for _ in range(100)
    ])
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            cursor.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM django_tasks_database_dbtaskresult "
                "WHERE status='READY' AND queue_name='tracking' AND backend_name='default' "
                "ORDER BY priority DESC, run_after LIMIT 1"
            )
            assert index_migration.INDEX_NAME in str(cursor.fetchall())
        constraints = connection.introspection.get_constraints(cursor, DBTaskResult._meta.db_table)
        assert constraints[index_migration.INDEX_NAME]["columns"] == [
            "queue_name", "backend_name", "priority", "run_after",
        ]
    for priority in (1, 5):
        DBTaskResult.objects.create(task_path="unused", queue_name="tracking", backend_name="default",
                                    args_kwargs={}, run_after=timezone.now(), priority=priority)
    assert DBTaskResult.objects.ready().filter(queue_name="tracking", backend_name="default").first().priority == 5
    with monkeypatch.context() as patch:
        patch.setattr(connection, "vendor", "postgresql")
        index_migration.drop_index(None, editor)
        index_migration.drop_index(None, editor)


def test_queue_index_skips_other_database_vendors():
    def unexpected_cursor():
        pytest.fail("PostgreSQL-only migration must not execute on another vendor")
    editor = SimpleNamespace(connection=SimpleNamespace(vendor="sqlite", cursor=unexpected_cursor))
    index_migration.create_index(None, editor)
    index_migration.drop_index(None, editor)
