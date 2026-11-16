"""Index reverse(normalized_email) so domain (suffix) lookups are range scans.

``LIKE '%@fsu.edu'`` cannot use a btree index, but the equivalent prefix
``reverse(normalized_email) LIKE 'ude.usf@%'`` can when the index uses
``text_pattern_ops`` (collation-independent prefix matching). The application
role owns ``contacts`` but cannot ``CREATE EXTENSION``, so this deliberately
avoids pg_trgm. Built CONCURRENTLY so contact writes keep flowing while the
index builds; SQLite (tests, local dev) needs no index for correctness.
"""

from django.db import migrations

INDEX_NAME = "contacts_norm_email_rev_idx"


def create_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        # A failed earlier CONCURRENTLY build leaves an INVALID index behind;
        # drop it so IF NOT EXISTS does not keep the broken one.
        cursor.execute(
            """
            SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
            WHERE c.relname = %s AND NOT i.indisvalid
            """,
            [INDEX_NAME],
        )
        if cursor.fetchone():
            cursor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
        cursor.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
            "ON contacts (reverse(normalized_email) text_pattern_ops)"
        )
        cursor.execute("ANALYZE contacts")


def drop_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("mailing", "0059_newsletter_templates_and_campaign"),
    ]

    operations = [
        migrations.RunPython(create_index, drop_index, elidable=False),
    ]
