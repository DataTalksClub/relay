import os
import socket
from uuid import uuid4

import boto3
import pytest

LOCALSTACK_ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", AWS_REGION)
os.environ.setdefault("AWS_REGION", AWS_REGION)
os.environ.setdefault("AWS_ENDPOINT_URL", LOCALSTACK_ENDPOINT)
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture
def aws_test_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_ENDPOINT_URL", LOCALSTACK_ENDPOINT)


@pytest.fixture
def localstack_available(aws_test_env):
    host, port = _host_port_from_endpoint(LOCALSTACK_ENDPOINT)
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        pytest.skip(f"LocalStack is not available at {LOCALSTACK_ENDPOINT}")


@pytest.fixture
def local_sqs_client(localstack_available):
    return boto3.client("sqs", region_name=AWS_REGION, endpoint_url=LOCALSTACK_ENDPOINT)


@pytest.fixture
def unique_queue_name():
    def build(prefix):
        return f"{prefix}-{uuid4().hex}"

    return build


def _host_port_from_endpoint(endpoint):
    without_scheme = endpoint.removeprefix("http://").removeprefix("https://")
    host_port = without_scheme.split("/", 1)[0]
    if ":" not in host_port:
        return host_port, 443 if endpoint.startswith("https://") else 80
    host, port = host_port.rsplit(":", 1)
    return host, int(port)


@pytest.fixture(autouse=True)
def _forbid_unscoped_joined_row_locks(monkeypatch):
    """Fail any query that joins with select_related and locks without ``of=``.

    Runtime half of ``tests/test_select_for_update_guard.py``: it catches
    querysets assembled over several statements, which the AST walk cannot
    follow. On PostgreSQL such a query locks every joined row (contacts,
    clients, ...) and deadlocked concurrent sends in production.
    """
    from django.db.models.sql.compiler import SQLCompiler  # noqa: PLC0415 - Django must be configured first

    original = SQLCompiler.as_sql

    def as_sql(self, *args, **kwargs):
        query = self.query
        if query.select_for_update and query.select_related and not query.select_for_update_of:
            raise AssertionError(
                f"select_for_update() on {query.model.__name__} joins select_related rows without of=; "
                "pass of=('self',) so only the intended rows are locked"
            )
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SQLCompiler, "as_sql", as_sql)


@pytest.fixture(autouse=True)
def _isolated_db_heartbeat_file(settings, tmp_path):
    """Keep loop heartbeats out of the shared /tmp default during tests."""
    settings.RELAY_DB_HEARTBEAT_FILE = str(tmp_path / "relay-db-heartbeat")
