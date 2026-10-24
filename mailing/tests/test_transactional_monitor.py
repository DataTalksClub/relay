from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.http import HttpResponse
from django.test import RequestFactory

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
    row = TransactionalRequestMinute.objects.create(client=client_record, minute=now, total=10, errors=2)
    assert monitor.client_alerts(client_record, now)[0]["kind"] == "api_errors"
    row.errors = 1
    row.save()
    assert monitor.client_alerts(client_record, now) == []
    row.errors = 10
    row.save()
    assert monitor.client_alerts(client_record, now + timedelta(minutes=15)) == []


def test_persisted_repeat_limit_and_delivery_failure_retry(client_record):
    now = datetime(2026, 10, 24, 12, tzinfo=UTC)
    seed_history(client_record, now - timedelta(hours=2))
    monitor.send_monitor_alert.side_effect = RuntimeError("secret must not be logged")
    monitor.check_transactional_health(now=now, force=True)
    monitor.send_monitor_alert.side_effect = None
    monitor.check_transactional_health(now=now + timedelta(minutes=1), force=True)
    monitor.check_transactional_health(now=now + timedelta(minutes=2), force=True)
    assert monitor.send_monitor_alert.call_count == 2


@pytest.mark.parametrize("status", [200, 400, 401, 403, 429, 500, 503])
def test_api_metrics_include_auth_failures_without_storing_token(client_record, status):
    key, raw = create_client_api_key(client=client_record, name="test")
    request = RequestFactory().post("/api/transactional/send", HTTP_AUTHORIZATION=f"Bearer {raw}")
    response = TransactionalMonitorMiddleware(lambda request: HttpResponse(status=status))(request)
    assert response.status_code == status
    metric = TransactionalRequestMinute.objects.get(client=client_record)
    assert (metric.total, metric.errors) == (1, int(status >= 400))
    assert raw not in str(metric.__dict__)


def test_test_command_has_fixed_recipient(monkeypatch, settings):
    # Exercise the actual sender independently of the fixture's mock.
    monkeypatch.setattr(monitor, "send_monitor_alert", real_send_monitor_alert)
    sender = Mock()
    monkeypatch.setattr(monitor, "send_email", sender)
    monkeypatch.setattr(monitor, "ses_client_for_source", Mock())
    settings.RELAY_TRANSACTIONAL_MONITOR_TO = "unwanted@example.invalid"
    call_command("check_transactional_health", test_alert=True)
    assert sender.call_args.kwargs["to_email"] == "dev@dtcdev.click"
