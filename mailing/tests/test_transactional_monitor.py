import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.http import HttpResponse
from django.test import RequestFactory

from mailing.dry_run import DryRunSuppressed
from mailing.models import (
    Client,
    Contact,
    EmailTemplate,
    Organization,
    TransactionalMessage,
    TransactionalRequestMinute,
)
from mailing.services import transactional_monitor as monitor
from mailing.services.auth import create_client_api_key
from mailing.transactional_monitor_middleware import TransactionalMonitorMiddleware

pytestmark = pytest.mark.django_db
real_send_monitor_alert = monitor.send_monitor_alert


@pytest.fixture
def client_record():
    org = Organization.objects.create(name="Test", slug="test")
    return Client.objects.create(organization=org, name="CMP", slug="cmp")


def seed_history(client, last):
    contact = Contact.objects.create(email="test@example.invalid")
    template = EmailTemplate.objects.create(client=client, key="confirmation", name="Confirmation", subject="Test")
    messages = [TransactionalMessage(client=client, contact=contact, email=contact.email,
                                    template=template, template_key=template.key, subject="Test") for _ in range(100)]
    TransactionalMessage.objects.bulk_create(messages)
    TransactionalMessage.objects.filter(client=client).update(created_at=last - timedelta(days=1))
    TransactionalMessage.objects.filter(pk=messages[-1].pk).update(created_at=last)


@pytest.fixture(autouse=True)
def isolate_monitor(monkeypatch, settings):
    monkeypatch.setattr(monitor, "_last_check", None)
    settings.RELAY_TRANSACTIONAL_MONITOR_ENABLED = True
    settings.RELAY_TRANSACTIONAL_MONITOR_CLIENTS = {}
    monkeypatch.setattr(monitor, "send_monitor_alert", Mock())


def test_incident_replay_pages_at_one_hour_and_through_outage(client_record):
    last = datetime(2026, 10, 22, 2, 21, 16, tzinfo=UTC)
    seed_history(client_record, last)
    assert monitor.check_transactional_health(now=last + timedelta(minutes=59), force=True) == []
    for hours in (1, 12, 24, 48):
        alerts = monitor.check_transactional_health(now=last + timedelta(hours=hours), force=True)
        assert alerts[0]["kind"] == "acceptance_gap"
    assert monitor.send_monitor_alert.call_count == 4
    # Arrival of the resend burst resolves the acceptance gap.
    TransactionalMessage.objects.filter(client=client_record).update(created_at=last + timedelta(hours=49))
    assert monitor.check_transactional_health(now=last + timedelta(hours=49), force=True) == []


def test_configurable_quiet_night_and_morning_grace(client_record, settings):
    last = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)
    seed_history(client_record, last)
    settings.RELAY_TRANSACTIONAL_MONITOR_CLIENTS = {str(client_record.pk): {"quiet_hours": [0, 1, 2, 3, 4, 5, 6, 7]}}
    for hour in (0, 4, 6):
        assert monitor.client_alerts(client_record, datetime(2026, 10, 23, hour, 30, tzinfo=UTC)) == []
    assert monitor.client_alerts(client_record, datetime(2026, 10, 23, 7, 0, tzinfo=UTC))


def test_new_client_and_low_baseline_never_page(client_record):
    now = datetime(2026, 10, 24, 12, tzinfo=UTC)
    assert monitor.client_alerts(client_record, now) == []
    seed_history(client_record, now - timedelta(hours=2))
    TransactionalMessage.objects.exclude(pk=TransactionalMessage.objects.first().pk).delete()
    assert monitor.client_alerts(client_record, now) == []


def test_error_threshold_not_silenced_at_night_and_expires(client_record, settings):
    now = datetime(2026, 10, 24, 0, tzinfo=UTC)
    settings.RELAY_TRANSACTIONAL_MONITOR_CLIENTS = {str(client_record.pk): {"quiet_hours": list(range(24))}}
    row = TransactionalRequestMinute.objects.create(
        client=client_record, minute=now, total=10, errors=2, server_errors=2)
    assert monitor.client_alerts(client_record, now)[0]["kind"] == "api_errors"
    row.server_errors = 1
    row.errors = 2
    row.save()
    assert monitor.client_alerts(client_record, now) == []
    row.errors = 10
    row.server_errors = 10
    row.save()
    assert monitor.client_alerts(client_record, now + timedelta(minutes=15)) == []


def test_single_4xx_probe_at_low_volume_does_not_page(client_record):
    # Replay of 2026-10-24 14:18-14:33 UTC for client 2: one 401 auth probe
    # among 5 requests paged before the 4xx/5xx split and must stay silent now.
    now = datetime(2026, 10, 24, 14, 33, tzinfo=UTC)
    TransactionalRequestMinute.objects.create(
        client=client_record, minute=datetime(2026, 10, 24, 14, 21, tzinfo=UTC),
        total=1, errors=0, server_errors=0)
    TransactionalRequestMinute.objects.create(
        client=client_record, minute=datetime(2026, 10, 24, 14, 22, tzinfo=UTC),
        total=3, errors=1, server_errors=0)
    TransactionalRequestMinute.objects.create(
        client=client_record, minute=datetime(2026, 10, 24, 14, 32, tzinfo=UTC),
        total=1, errors=0, server_errors=0)
    assert monitor.client_alerts(client_record, now) == []


def test_single_500_pages_at_low_volume(client_record):
    now = datetime(2026, 10, 24, 12, tzinfo=UTC)
    TransactionalRequestMinute.objects.create(
        client=client_record, minute=now, total=5, errors=1, server_errors=1)
    alerts = monitor.client_alerts(client_record, now)
    assert alerts[0]["kind"] == "api_errors"
    assert alerts[0]["server_errors"] == 1


def test_4xx_flood_at_volume_pages(client_record):
    now = datetime(2026, 10, 24, 12, tzinfo=UTC)
    TransactionalRequestMinute.objects.create(
        client=client_record, minute=now, total=20, errors=5, server_errors=0)
    assert monitor.client_alerts(client_record, now)[0]["kind"] == "api_errors"


@pytest.mark.parametrize("errors,expected", [(3, False), (4, True)])
def test_mixed_4xx_and_5xx_use_combined_error_threshold(client_record, errors, expected):
    now = datetime(2026, 10, 24, 12, tzinfo=UTC)
    TransactionalRequestMinute.objects.create(
        client=client_record, minute=now, total=20, errors=errors, server_errors=2)
    alerts = monitor.client_alerts(client_record, now)
    assert bool(alerts) is expected
    if expected:
        assert alerts[0]["kind"] == "api_errors"
        assert alerts[0]["error_percent"] == 20


def test_full_4xx_at_low_volume_does_not_page(client_record):
    # Even 100% 4xx stays silent below the flood volume: a total stoppage with
    # zero acceptances is covered by the acceptance-gap check instead.
    now = datetime(2026, 10, 24, 12, tzinfo=UTC)
    TransactionalRequestMinute.objects.create(
        client=client_record, minute=now, total=10, errors=10, server_errors=0)
    assert monitor.client_alerts(client_record, now) == []


def test_persisted_repeat_limit_and_delivery_failure_retry(client_record):
    now = datetime(2026, 10, 24, 12, tzinfo=UTC)
    seed_history(client_record, now - timedelta(hours=2))
    monitor.send_monitor_alert.side_effect = RuntimeError("secret must not be logged")
    monitor.check_transactional_health(now=now, force=True)
    monitor.send_monitor_alert.side_effect = None
    monitor.check_transactional_health(now=now + timedelta(minutes=1), force=True)
    monitor.check_transactional_health(now=now + timedelta(minutes=2), force=True)
    assert monitor.send_monitor_alert.call_count == 2


@pytest.mark.parametrize("status", [200, 400, 401, 403, 404, 429, 500, 503])
def test_api_metrics_include_auth_failures_without_storing_token(client_record, status):
    key, raw = create_client_api_key(client=client_record, name="test")
    request = RequestFactory().post("/api/transactional/send", HTTP_AUTHORIZATION=f"Bearer {raw}")
    response = TransactionalMonitorMiddleware(lambda request: HttpResponse(status=status))(request)
    assert response.status_code == status
    metric = TransactionalRequestMinute.objects.get(client=client_record)
    assert (metric.total, metric.errors) == (1, int(status >= 400))
    assert metric.server_errors == int(status >= 500)
    assert raw not in str(metric.__dict__)


@pytest.mark.parametrize("missing", ["key", "inactive", "version", "lookup"])
def test_template_missing_404_keeps_http_contract_without_api_error(client, client_record, missing):
    _, raw = create_client_api_key(client=client_record, name="reminders")
    key = "registration-confirmation-reminder-1"
    if missing in {"inactive", "version"}:
        EmailTemplate.objects.create(
            client=client_record, key=key, name="Reminder", subject="Confirm",
            is_active=missing != "inactive",
        )
    headers = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
    payload = {"email": "test@example.invalid", "template_key": key}
    if missing == "version":
        payload["template_version"] = 1
    if missing == "lookup":
        response = client.get(f"/api/transactional/templates/{key}", **headers)
    else:
        response = client.post("/api/transactional/send", payload, content_type="application/json", **headers)
    field = "template_version" if missing == "version" else "template_key"
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "validation_error", "fields": {field: "not_found"}}}
    metric = TransactionalRequestMinute.objects.get(client=client_record)
    assert (metric.total, metric.errors, metric.server_errors) == (1, 0, 0)
    assert not TransactionalMessage.objects.exists()


def test_missing_template_flood_does_not_page_but_other_4xx_still_do(client, client_record):
    _, raw = create_client_api_key(client=client_record, name="reminders")
    headers = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
    for _ in range(20):
        response = client.post(
            "/api/transactional/send",
            {"email": "test@example.invalid", "template_key": "registration-confirmation-reminder-2"},
            content_type="application/json", **headers,
        )
        assert response.status_code == 404
    now = datetime.now(UTC)
    assert monitor.client_alerts(client_record, now) == []
    for _ in range(5):
        response = client.get("/api/transactional/messages/999999", **headers)
        assert response.status_code == 404
    alert = monitor.client_alerts(client_record, datetime.now(UTC))[0]
    assert (alert["requests"], alert["errors"], alert["server_errors"]) == (25, 5, 0)
    assert alert["error_percent"] == 20


def test_test_command_has_fixed_recipient(monkeypatch, settings):
    # Exercise the actual sender independently of the fixture's mock.
    monkeypatch.setattr(monitor, "send_monitor_alert", real_send_monitor_alert)
    sender = Mock()
    monkeypatch.setattr(monitor, "send_email", sender)
    monkeypatch.setattr(monitor, "ses_client_for_source", Mock())
    settings.RELAY_TRANSACTIONAL_MONITOR_TO = "unwanted@example.invalid"
    call_command("check_transactional_health", test_alert=True)
    assert sender.call_args.kwargs["to_email"] == "dev@dtcdev.click"


def test_ops_transport_uses_existing_config_and_fixed_test_recipient(monkeypatch, settings):
    settings.RELAY_TRANSACTIONAL_MONITOR_MSMTP_CONFIG = "/run/relay-ops-msmtprc"
    settings.RELAY_TRANSACTIONAL_MONITOR_TO = "unwanted@example.invalid"
    monkeypatch.delenv("RELAY_DRY_RUN", raising=False)
    transport = Mock()
    monkeypatch.setattr(monitor.subprocess, "run", transport)
    message_id = real_send_monitor_alert({"kind": "forced_test"}, test=True)
    command = transport.call_args.args[0]
    assert command == ["/usr/bin/msmtp", "--file=/run/relay-ops-msmtprc",
                       "--from=" + settings.RELAY_TRANSACTIONAL_MONITOR_FROM,
                       "--timeout=15", "--", "dev@dtcdev.click"]
    assert b"To: dev@dtcdev.click" in transport.call_args.kwargs["input"]
    assert b"unwanted@example.invalid" not in transport.call_args.kwargs["input"]
    assert transport.call_args.kwargs["timeout"] == 20
    assert message_id.endswith("@relay.datatalks.club>")


def test_ops_transport_obeys_enforced_dry_run(monkeypatch, settings):
    settings.RELAY_TRANSACTIONAL_MONITOR_MSMTP_CONFIG = "/run/relay-ops-msmtprc"
    monkeypatch.setenv("RELAY_DRY_RUN", "1")
    transport = Mock()
    monkeypatch.setattr(monitor.subprocess, "run", transport)
    with pytest.raises(DryRunSuppressed):
        real_send_monitor_alert({"kind": "forced_test"}, test=True)
    transport.assert_not_called()


def test_ops_transport_sanitizes_failure_diagnostics(monkeypatch, settings):
    settings.RELAY_TRANSACTIONAL_MONITOR_MSMTP_CONFIG = "/run/relay-ops-msmtprc"
    monkeypatch.delenv("RELAY_DRY_RUN", raising=False)
    failure = subprocess.CalledProcessError(1, ["msmtp"], stderr=b"private diagnostic")
    monkeypatch.setattr(monitor.subprocess, "run", Mock(side_effect=failure))
    with pytest.raises(RuntimeError, match="^ops_alert_transport_failed$"):
        real_send_monitor_alert({"kind": "forced_test"}, test=True)
