"""Return pooled connections during network calls and rate-limit sleeps."""

from django.db import connection


def release_pooled_connection():
    # Never close a transaction's connection or alter unpooled send paths.
    # Django close() returns the socket to psycopg_pool; the next ORM query
    # checks it out again without a PostgreSQL handshake.
    if connection.settings_dict.get("OPTIONS", {}).get("pool") and not connection.in_atomic_block:
        connection.close()
