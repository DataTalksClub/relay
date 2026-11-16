"""Index reverse(normalized_email) so domain (suffix) lookups are range scans.

``LIKE '%@fsu.edu'`` cannot use a btree index, but the equivalent prefix
``reverse(normalized_email) LIKE 'ude.usf@%'`` can when the index uses
``text_pattern_ops`` (collation-independent prefix matching). The application
role owns ``contacts`` but cannot ``CREATE EXTENSION``, so this deliberately
avoids pg_trgm. SQLite (tests, local dev) needs no index for correctness.

Not CONCURRENTLY: containers start together and run migrate_before_start,
which serializes them on a session advisory lock. CREATE INDEX CONCURRENTLY
waits for every open transaction, including a sibling container's transaction
blocked on that advisory lock, which deadlocks. A plain build of the
~236k-row table takes about a second under a SHARE lock (contact writes wait,
they do not fail) and rolls back cleanly with the migration transaction.
"""

from django.db import migrations

INDEX_NAME = "contacts_norm_email_rev_idx"


def create_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON contacts (reverse(normalized_email) text_pattern_ops)"
        )
        cursor.execute("ANALYZE contacts")


def drop_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")


class Migration(migrations.Migration):
    dependencies = [
        ("mailing", "0059_newsletter_templates_and_campaign"),
    ]

    operations = [
        migrations.RunPython(create_index, drop_index, elidable=False),
    ]
