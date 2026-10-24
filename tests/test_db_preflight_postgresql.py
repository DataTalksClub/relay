"""Real concurrent DDL and ownership rejection on a disposable PostgreSQL DB.

RELAY_TEST_POSTGRES_URL must name an empty, disposable database, never DEV/prod.
"""

import os
import subprocess
from urllib.parse import urlparse

import psycopg2
import pytest

ROOT = os.path.dirname(os.path.dirname(__file__))
URL = os.environ.get("RELAY_TEST_POSTGRES_URL")


@pytest.mark.skipif(not URL, reason="Requires disposable RELAY_TEST_POSTGRES_URL")
def test_real_postgresql_concurrent_start_noop_and_wrong_owner():
    parsed = urlparse(URL)
    role, database = parsed.username, parsed.path.lstrip("/")
    assert database.startswith("relay_preflight_test"), "Never run against a serving database"
    env = {**os.environ, "DATABASE_URL": URL}

    def launch(*args):
        return subprocess.Popen(
            [os.sys.executable, "manage.py", *args], cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

    # Two processes start from an empty DB; both would race CREATE TABLE
    # django_migrations and the same DDL without the session advisory lock.
    runners = [launch("migrate_before_start") for _ in range(2)]
    for runner in runners:
        output, _ = runner.communicate(timeout=180)
        assert runner.returncode == 0, output
    for command in [("migrate_before_start",), ("migrate", "--noinput")]:
        runner = launch(*command)
        output, _ = runner.communicate(timeout=60)
        assert runner.returncode == 0, output
        assert "No migrations to apply" in output

    with psycopg2.connect(URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE ROLE restore_owner")
            cursor.execute("GRANT CREATE ON SCHEMA public TO restore_owner")
            cursor.execute("SET ROLE restore_owner")
            cursor.execute("CREATE TABLE public.restored_parent (id int) PARTITION BY RANGE (id)")
            cursor.execute("CREATE TABLE public.restored_partition PARTITION OF restored_parent FOR VALUES FROM (0) TO (10)")
            cursor.execute("CREATE INDEX restored_index ON public.restored_parent (id)")
            cursor.execute("CREATE SEQUENCE public.restored_sequence")
            cursor.execute("CREATE VIEW public.restored_view AS SELECT 1 AS id")
            cursor.execute("CREATE FUNCTION public.restored_function() RETURNS int LANGUAGE SQL AS 'SELECT 1'")
    args = ("db_preflight", "--expected-role", role, "--expected-database", database)
    runner = launch(*args)
    output, _ = runner.communicate(timeout=60)
    assert runner.returncode != 0
    for name in ["restored_parent", "restored_partition", "restored_index", "restored_sequence", "restored_view", "restored_function"]:
        assert name in output
    assert "Migration plan before rolling switch" not in output

    # Align test fixtures without deleting data, just like the restore runbook.
    with psycopg2.connect(URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f'REASSIGN OWNED BY restore_owner TO "{role}"')
    runner = launch(*args)
    output, _ = runner.communicate(timeout=60)
    assert runner.returncode == 0, output
    assert "DB preflight PASS" in output
