"""Keep a busy task queue from making other lanes scan its entire backlog.

The upstream READY index orders all queues together. Under a large provider
backlog, polls of other queues scan those unrelated tasks before returning.
Use a queue-first partial index for both ordered claims and existence checks.
As with migration 0060, use a transactional build: concurrent builds can
deadlock with sibling startup migrations waiting on the advisory lock.
"""

from django.db import migrations

INDEX_NAME = "tasks_db_ready_queue_order_idx"


def create_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
            "ON django_tasks_database_dbtaskresult "
            "(queue_name, backend_name, priority DESC, run_after) WHERE status = 'READY'"
        )
        cursor.execute("ANALYZE django_tasks_database_dbtaskresult")


def drop_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")


class Migration(migrations.Migration):
    dependencies = [
        ("mailing", "0061_send_fingerprints"),
        ("django_tasks_database", "0019_rename_django_task_new_ordering_idx_tasks_db_new_ordering_idx_and_more"),
    ]

    operations = [migrations.RunPython(create_index, drop_index, elidable=False)]
