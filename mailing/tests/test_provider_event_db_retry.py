"""A transient DB error must never leave a provider event permanently FAILED.

2026-10-27 10:43Z, production: one ``deadlock detected`` in
``correlate_ses_message`` failed provider-events task d3cf093c and the SES
event was lost, because django-tasks-db never retries a FAILED task. The
provider-event tasks now retry transient DB errors in-process and then as
delayed tasks with bounded backoff; SES processing stays idempotent on
``provider_event_id`` so a retry cannot double-apply an event.
"""

import logging
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.db import OperationalError
from django.tasks import TaskResultStatus
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

import mailing.services.ses_webhooks as ses_webhooks
from mailing import db_retry
from mailing import tasks as mailing_tasks
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

pytestmark = pytest.mark.django_db(transaction=True)

DEADLOCK = 'deadlock detected\nDETAIL:  Process 1 waits for ShareLock on transaction 2\nCONTEXT:  while locking tuple (0,1) in relation "contacts"'


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(db_retry, "sleep", sleeps.append)
    return sleeps


@pytest.fixture
def message():
    organization = Organization.objects.create(name="Retry", slug="retry")
    client = Client.objects.create(organization=organization, name="News", slug="news")
    contact = Contact.objects.create(email="retry@example.invalid")
    template = EmailTemplate.objects.create(
        client=client, key="verify", name="Verify", subject="S", html_body="<p>B</p>", text_body="B"
    )
    return TransactionalMessage.objects.create(
        client=client,
        contact=contact,
        email=contact.normalized_email,
        from_email_id="news",
        from_email="news@example.invalid",
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.SENT,
        idempotency_key="retry-1",
        subject="S",
        html_body="<p>B</p>",
        text_body="B",
        ses_message_id="ses-retry-1",
        sent_at=timezone.now(),
    )


def _payload(provider_event_id="sns-retry-delivery-1"):
    return {
        "contract": "ses-webhooks",
        "version": 1,
        "provider": "ses",
        "provider_event_id": provider_event_id,
        "notification_type": "delivery",
        "received_at": "2026-10-27T10:43:07Z",
        "ses_message_id": "ses-retry-1",
        "mail_message_id": "ses-retry-1",
        "metadata": {},
    }


def _fail_correlation(monkeypatch, failures):
    """Raise ``failures`` (in order) from inside the locking transaction."""
    original = ses_webhooks.correlate_ses_message
    remaining = list(failures)

    def correlate(ses_message_id):
        if remaining:
            raise remaining.pop(0)
        return original(ses_message_id)

    monkeypatch.setattr(ses_webhooks, "correlate_ses_message", correlate)


def test_a_deadlock_is_retried_in_process_and_applied_once(monkeypatch, message, no_sleep):
    _fail_correlation(monkeypatch, [OperationalError(DEADLOCK)])

    result = mailing_tasks.process_ses_webhook_event.call(_payload())

    event = EmailEvent.objects.get(provider_event_id="sns-retry-delivery-1")
    assert result == {"email_event_id": event.pk, "event_type": EmailEventType.DELIVERED}
    # 0.5s base step with up to +25% jitter.
    assert len(no_sleep) == 1
    assert 0.5 <= no_sleep[0] <= 0.625
    message.refresh_from_db()
    assert message.delivered_at is not None
    assert not DBTaskResult.objects.exists()


def test_a_persistent_transient_error_defers_to_a_delayed_task(monkeypatch, message, caplog):
    outage = OperationalError('could not translate host name "mqimefrti7ratmaliirnikov" to address')
    _fail_correlation(monkeypatch, [outage] * 4)
    before = timezone.now()

    with caplog.at_level(logging.WARNING, logger="mailing"):
        result = mailing_tasks.process_ses_webhook_event.call(_payload())

    # 30s base step with up to +25% jitter.
    assert 30 <= result["retry_in_seconds"] <= 37.5
    assert result["deferred"] is True
    assert result["next_attempt"] == 2
    assert not EmailEvent.objects.exists()
    retry = DBTaskResult.objects.get()
    assert retry.task_path == "mailing.tasks.process_ses_webhook_event"
    assert retry.queue_name == "provider-events"
    assert retry.status == TaskResultStatus.READY
    assert retry.args_kwargs["kwargs"] == {"attempt": 2}
    assert retry.args_kwargs["args"] == [_payload()]
    assert retry.run_after >= before + timedelta(seconds=30)
    deferral = [r.getMessage() for r in caplog.records if "as a delayed task" in r.getMessage()]
    assert deferral == [
        "process_ses_webhook_event: retrying transient DB error as a delayed task "
        f'(next attempt 2/7 in {int(result["retry_in_seconds"])}s): '
        'could not translate host name "mqimefrti7ratmaliirnikov" to address'
    ]
    # The watchdog restarts on these class paths in worker logs; a handled
    # transient error must not print them.
    assert not any("OperationalError" in r.getMessage() for r in caplog.records)


def test_the_delayed_attempt_applies_the_event_through_the_worker(monkeypatch, message):
    _fail_correlation(monkeypatch, [OperationalError(DEADLOCK)] * 4)
    mailing_tasks.process_ses_webhook_event.enqueue(_payload())

    call_command("db_worker", "--queue-name", "provider-events", "--batch", "--interval", "0", "--verbosity", "0")
    first = DBTaskResult.objects.get(args_kwargs__kwargs={})
    assert first.status == TaskResultStatus.SUCCESSFUL
    assert first.return_value["deferred"] is True
    assert first.return_value["next_attempt"] == 2
    assert 30 <= first.return_value["retry_in_seconds"] <= 900

    DBTaskResult.objects.filter(status=TaskResultStatus.READY).update(run_after=timezone.now())
    call_command("db_worker", "--queue-name", "provider-events", "--batch", "--interval", "0", "--verbosity", "0")

    second = DBTaskResult.objects.get(args_kwargs__kwargs={"attempt": 2})
    assert second.status == TaskResultStatus.SUCCESSFUL
    assert EmailEvent.objects.filter(provider_event_id="sns-retry-delivery-1").count() == 1
    assert not DBTaskResult.objects.filter(status=TaskResultStatus.FAILED).exists()


def test_backoff_is_bounded_and_the_last_attempt_fails_loudly(monkeypatch, message, settings):
    settings.RELAY_PROVIDER_EVENT_MAX_ATTEMPTS = 3
    _fail_correlation(monkeypatch, [OperationalError(DEADLOCK)] * 4)

    with pytest.raises(OperationalError, match="deadlock detected"):
        mailing_tasks.process_ses_webhook_event.call(_payload(), attempt=3)
    assert not DBTaskResult.objects.exists()

    _fail_correlation(monkeypatch, [OperationalError(DEADLOCK)] * 4)
    assert mailing_tasks.process_ses_webhook_event.call(_payload(), attempt=2)["retry_in_seconds"] <= 75
    settings.RELAY_PROVIDER_EVENT_MAX_ATTEMPTS = 20
    _fail_correlation(monkeypatch, [OperationalError(DEADLOCK)] * 4)
    assert mailing_tasks.process_ses_webhook_event.call(_payload(), attempt=10)["retry_in_seconds"] == 900


def test_a_permanent_db_error_is_not_retried(monkeypatch, message, no_sleep):
    _fail_correlation(monkeypatch, [OperationalError('relation "contacts" does not exist')])

    with pytest.raises(OperationalError, match="does not exist"):
        mailing_tasks.process_ses_webhook_event.call(_payload())
    assert no_sleep == []
    assert not DBTaskResult.objects.exists()


def test_reprocessing_an_applied_event_is_a_no_op(message):
    first = mailing_tasks.process_ses_webhook_event.call(_payload())
    message.refresh_from_db()
    delivered_at = message.delivered_at

    assert mailing_tasks.process_ses_webhook_event.call(_payload()) is None

    assert first["email_event_id"] == EmailEvent.objects.get(provider_event_id="sns-retry-delivery-1").pk
    assert EmailEvent.objects.count() == 1
    message.refresh_from_db()
    assert message.delivered_at == delivered_at


def test_inbound_email_task_retries_transient_errors(monkeypatch):
    calls = []

    def process(payload):
        calls.append(payload)
        if len(calls) == 1:
            raise OperationalError("server closed the connection unexpectedly")
        return [{"message_id": "m-1"}]

    monkeypatch.setattr(mailing_tasks, "process_inbound_s3_notification", process)

    assert mailing_tasks.process_inbound_email.call({"Records": []}) == [{"message_id": "m-1"}]
    assert len(calls) == 2
