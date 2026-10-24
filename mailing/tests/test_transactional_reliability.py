from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from botocore.exceptions import EndpointConnectionError
from django.core.management import call_command
from django.db import ProgrammingError
from django.utils import timezone
from django_tasks_db.management.commands.db_worker import Worker
from django_tasks_db.models import DBTaskResult

from mailing import tasks
from mailing.models import (
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    Organization,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services import transactional_reliability as reliability
from mailing.services.transactional import build_transactional_queue_payload
from mailing.services.transactional_sender import TransientSendFailure

pytestmark = pytest.mark.django_db


@pytest.fixture
def message():
    organization = Organization.objects.create(name="Retry", slug="retry")
    client = Client.objects.create(organization=organization, name="Retry", slug="retry")
    contact = Contact.objects.create(email="private@example.com")
    template = EmailTemplate.objects.create(client=client, key="retry", name="Retry", subject="Test")
    return TransactionalMessage.objects.create(
        client=client, contact=contact, email=contact.email,
        template=template, template_key=template.key,
        status=TransactionalMessageStatus.QUEUED, idempotency_key="retry-1",
        subject="Test", html_body="<p>Test</p>", text_body="Test",
    )


@pytest.fixture(autouse=True)
def reset_alert_clock(monkeypatch):
    monkeypatch.setattr(reliability, "_last_alert_at", None)


@pytest.mark.parametrize("error", [TransientSendFailure("transient"), ProgrammingError("missing column")])
def test_failed_send_schedules_bounded_backoff_in_database(message, settings, monkeypatch, error):
    settings.RELAY_TRANSACTIONAL_MAX_ATTEMPTS = 5
    settings.RELAY_TRANSACTIONAL_RETRY_BASE_SECONDS = 60
    settings.RELAY_TRANSACTIONAL_RETRY_MAX_SECONDS = 200
    now = timezone.now()
    monkeypatch.setattr("mailing.tasks.timezone.now", lambda: now)
    sender = Mock(side_effect=error)
    monkeypatch.setattr(tasks, "send_transactional_email_from_queue", sender)
    payload = build_transactional_queue_payload(message)

    for attempt in range(1, 6):
        with pytest.raises(type(error)):
            tasks.send_transactional_email.call(payload, attempt=attempt)

    retries = list(DBTaskResult.objects.filter(task_path="mailing.tasks.send_transactional_email").order_by("run_after", "enqueued_at"))
    assert len(retries) == 4
    assert [task.args_kwargs["kwargs"]["attempt"] for task in retries] == [2, 3, 4, 5]
    assert [(task.run_after - now).total_seconds() for task in retries] == [60, 120, 200, 200]
    assert all(task.args_kwargs["args"] == [payload] for task in retries)
    message.refresh_from_db()
    assert message.status == TransactionalMessageStatus.FAILED
    event = EmailEvent.objects.get(transactional_message=message, event_type=EmailEventType.FAILED)
    assert event.metadata["reason"] == "retry_exhausted"
    assert event.metadata["attempts"] == 5


def test_terminal_message_retry_never_sends_again(message, monkeypatch):
    message.status = TransactionalMessageStatus.SENT
    message.ses_message_id = "already-accepted"
    message.save()
    ses = Mock(side_effect=AssertionError("Must not contact SES"))
    monkeypatch.setattr("mailing.services.transactional_sender.ses_client_for_source", ses)
    result = tasks.send_transactional_email.call(build_transactional_queue_payload(message), attempt=2)
    assert result["status"] == "sent"
    assert not ses.called
    assert DBTaskResult.objects.count() == 0


def test_database_worker_keeps_failed_attempt_and_successful_retry(message, monkeypatch):
    send = Mock(side_effect=[EndpointConnectionError(endpoint_url="https://ses.invalid"), "ses-accepted"])
    monkeypatch.setattr("mailing.services.transactional_sender.ses_client_for_source", lambda source: object())
    monkeypatch.setattr("mailing.services.transactional_sender.send_email", send)
    result = tasks.send_transactional_email.enqueue(build_transactional_queue_payload(message))
    worker = Worker(
        queue_names=["default"], interval=0, batch=True, backend_name="default",
        startup_delay=False, max_tasks=1, worker_id="retry-test",
    )
    initial = DBTaskResult.objects.get(pk=result.id)
    initial.claim(worker.worker_id)
    worker.run_task(initial)
    initial.refresh_from_db()
    assert initial.status == "FAILED"
    message.refresh_from_db()
    assert message.status == TransactionalMessageStatus.QUEUED
    retry = DBTaskResult.objects.exclude(pk=initial.pk).get()
    assert retry.run_after > timezone.now()
    assert not DBTaskResult.objects.ready().exists()
    retry.claim(worker.worker_id)
    worker.run_task(retry)
    retry.refresh_from_db()
    message.refresh_from_db()
    assert retry.status == "SUCCESSFUL"
    assert message.status == TransactionalMessageStatus.SENT
    assert message.ses_message_id == "ses-accepted"
    assert send.call_count == 2
    assert EmailEvent.objects.filter(transactional_message=message, event_type=EmailEventType.SENT).count() == 1


def test_uncertain_sending_is_never_released_and_eventually_failed(message, settings, monkeypatch):
    message.status = TransactionalMessageStatus.SENDING
    message.save()
    settings.RELAY_TRANSACTIONAL_MAX_ATTEMPTS = 2
    ses = Mock(side_effect=AssertionError("Must not contact SES"))
    monkeypatch.setattr("mailing.services.transactional_sender.ses_client_for_source", ses)
    payload = build_transactional_queue_payload(message)
    with pytest.raises(TransientSendFailure):
        tasks.send_transactional_email.call(payload)
    message.refresh_from_db()
    assert message.status == TransactionalMessageStatus.SENDING
    with pytest.raises(TransientSendFailure):
        tasks.send_transactional_email.call(payload, attempt=2)
    message.refresh_from_db()
    assert message.status == TransactionalMessageStatus.FAILED
    assert "delivery outcome uncertain" in message.last_error
    assert EmailEvent.objects.get(transactional_message=message).metadata["delivery_uncertain"] is True
    assert not ses.called


def test_stuck_alert_uses_creation_age_and_redacts_payload(message, settings, caplog, monkeypatch):
    settings.RELAY_TRANSACTIONAL_QUEUED_ALERT_MINUTES = 10
    settings.RELAY_TRANSACTIONAL_ALERT_REPEAT_SECONDS = 300
    settings.RELAY_TRANSACTIONAL_ALERT_WEBHOOK_URL = "https://alerts.invalid/private-token"
    now = timezone.now()
    TransactionalMessage.objects.filter(pk=message.pk).update(
        created_at=now - timedelta(minutes=11), updated_at=now,
    )
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    webhook = Mock(return_value=response)
    monkeypatch.setattr(reliability, "urlopen", webhook)
    assert reliability.alert_stuck_transactional_messages(now=now) == 1
    assert reliability.alert_stuck_transactional_messages(now=now + timedelta(seconds=299)) == 0
    assert reliability.alert_stuck_transactional_messages(now=now + timedelta(seconds=300)) == 1
    assert webhook.call_count == 2
    body = webhook.call_args.args[0].data.decode()
    assert '"message_ids": [' + str(message.pk) + ']' in body
    assert "relay_transactional_queued_too_long" in caplog.text
    assert message.email not in caplog.text + body
    assert message.idempotency_key not in caplog.text + body
    assert "private-token" not in caplog.text
    message.refresh_from_db()
    assert message.status == TransactionalMessageStatus.QUEUED
    assert DBTaskResult.objects.count() == 0


def test_fresh_or_terminal_messages_do_not_alert(message, settings, caplog):
    settings.RELAY_TRANSACTIONAL_QUEUED_ALERT_MINUTES = 5
    assert reliability.alert_stuck_transactional_messages() == 0
    TransactionalMessage.objects.filter(pk=message.pk).update(
        status=TransactionalMessageStatus.SENT, created_at=timezone.now() - timedelta(days=1),
    )
    assert reliability.alert_stuck_transactional_messages() == 0
    assert not caplog.records


@pytest.mark.parametrize("webhook_url", ["https://alerts.invalid/secret-token", "malformed-secret-token"])
def test_alert_webhook_failure_does_not_stop_scheduler_or_leak_url(message, settings, caplog, monkeypatch, webhook_url):
    settings.RELAY_TRANSACTIONAL_QUEUED_ALERT_MINUTES = 1
    settings.RELAY_TRANSACTIONAL_ALERT_WEBHOOK_URL = webhook_url
    TransactionalMessage.objects.filter(pk=message.pk).update(created_at=timezone.now() - timedelta(minutes=2))
    monkeypatch.setattr(reliability, "urlopen", Mock(side_effect=OSError("https://alerts.invalid/secret-token")))
    assert reliability.alert_stuck_transactional_messages() == 1
    assert "transactional_queued_alert_webhook_failed" in caplog.text
    assert "secret-token" not in caplog.text


def test_scheduler_checks_queued_age_every_pass():
    with patch("jobs.management.commands.run_relay_scheduler.alert_stuck_transactional_messages") as alert:
        call_command("run_relay_scheduler", once=True, verbosity=0)
    alert.assert_called_once_with()
