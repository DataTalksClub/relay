"""Render actual Gunicorn records, including sensitive paths and headers."""

import io
import logging
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from gunicorn.config import Config

from relay.gunicorn_logging import ACCESS_LOG_FORMAT, PrivateAccessLogger


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/health/ready", "/health/ready"),
        ("/t/o/secret-recipient.gif", "/t/o/[tracking_token].gif"),
        ("/t/c/secret-recipient", "/t/c/[tracking_token]"),
        ("/unsubscribe/secret-recipient", "/unsubscribe/[unsubscribe_token]"),
        ("/contacts/person@example.com/tags/add/", "/contacts/[contact_email]/tags/add/"),
        ("/contacts/person%40example.com/", "/contacts/[contact_email]/"),
        ("/api/campaigns/secret-recipient/preview", "/api/campaigns/[external_key]/preview"),
        ("/unknown/person@example.com/secret-recipient", "/[unmatched]"),
    ],
)
@pytest.mark.parametrize(
    "request_id", [None, "person@example.com", "secret-recipient", "61a8fa3e-045b-440e-99b0-3445fe197368"]
)
def test_access_record_privacy_and_duration(path, expected, request_id):
    cfg = Config()
    cfg.set("accesslog", "-")
    cfg.set("access_log_format", ACCESS_LOG_FORMAT)
    logger = PrivateAccessLogger(cfg)
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    logger.access_log.handlers = [handler]
    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": path,
        "RAW_URI": path + "?email=person@example.com&token=secret-recipient",
        "QUERY_STRING": "email=person@example.com&token=secret-recipient",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "REMOTE_ADDR": "192.0.2.1",
        "HTTP_REFERER": "https://example.com/?token=secret-recipient",
        "HTTP_USER_AGENT": "person@example.com",
        "REMOTE_USER": "person@example.com",
        "HTTP_AUTHORIZATION": "Bearer secret-recipient",
    }
    if request_id is not None:
        environ["HTTP_X_REQUEST_ID"] = request_id
    resp = SimpleNamespace(status="200 OK", sent=42, headers=[])
    logger.access(resp, SimpleNamespace(headers=[]), environ, timedelta(microseconds=123456))
    line = output.getvalue()
    assert f'"GET {expected} HTTP/1.1" 200 42 123456 request_id="' in line
    actual_id = line.split('request_id="')[1].split('"')[0]
    assert str(UUID(actual_id)) == actual_id
    if request_id == "61a8fa3e-045b-440e-99b0-3445fe197368":
        assert actual_id == request_id
    for secret in ("?", "person", "example.com", "secret-recipient", "192.0.2.1", "Bearer"):
        assert secret not in line
    # Adding a raw atom to the format later still cannot expose its value.
    atoms = logger.atoms(resp, SimpleNamespace(headers=[]), environ, timedelta())
    assert "r" not in atoms
    assert "q" not in atoms
    assert "{authorization}i" not in atoms


def test_default_gunicorn_config_loads_policy(monkeypatch):
    from gunicorn.app.wsgiapp import WSGIApplication  # noqa: PLC0415

    monkeypatch.setattr("sys.argv", ["gunicorn", "relay.wsgi:application"])
    app = WSGIApplication()
    assert app.cfg.access_log_format == ACCESS_LOG_FORMAT
    assert app.cfg.logger_class is PrivateAccessLogger
