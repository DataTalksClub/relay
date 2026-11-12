from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest.mock import MagicMock

import pytest
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

import mailing.services.ses_monitor as ses_monitor_module
from mailing.models import (
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    OperatorAudit,
    Organization,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.ses_monitor import (
    check_ses_delivery_gap,
    check_ses_outcome_gap,
    check_ses_transport_health,
    check_sns_persisted_gap,
)


@pytest.fixture
def org_obj(db):
    return Organization.objects.create(name="Test Org", slug="test-org")


@pytest.fixture
def client_obj(org_obj):
    return Client.objects.create(organization=org_obj, name="Test Client", is_active=True)


@pytest.fixture
def contact_obj(db):
    return Contact.objects.create(email="user@example.com")


@pytest.fixture
def template_obj(client_obj):
    return EmailTemplate.objects.create(
        client=client_obj,
        key="welcome",
        name="Welcome",
        subject="Welcome",
    )


@pytest.mark.django_db
def test_check_ses_outcome_gap_detects_unconfirmed_messages(client_obj, contact_obj, template_obj):
    now = timezone.now()
    # Message sent 3 hours ago with no provider outcome event
    stuck = TransactionalMessage.objects.create(
        client=client_obj,
        contact=contact_obj,
        template=template_obj,
        email=contact_obj.email,
        template_key="welcome",
        status=TransactionalMessageStatus.SENT,
        ses_message_id="ses-stuck-1",
        delivered_at=None,
    )
    TransactionalMessage.objects.filter(pk=stuck.pk).update(created_at=now - timedelta(hours=3))

    alert = check_ses_outcome_gap(now, threshold=1)
    assert alert is not None
    assert alert["kind"] == "ses_outcome_gap"
    assert alert["count"] == 1
    assert alert["message_ids"] == [stuck.pk]


@pytest.mark.django_db
def test_check_ses_outcome_gap_ignores_confirmed_or_young_messages(client_obj, contact_obj, template_obj):
    now = timezone.now()

    # 1. Message sent 1 hour ago (younger than 2h)
    m1 = TransactionalMessage.objects.create(
        client=client_obj,
        contact=contact_obj,
        template=template_obj,
        email="m1@example.com",
        template_key="welcome",
        status=TransactionalMessageStatus.SENT,
        ses_message_id="ses-1",
    )
    TransactionalMessage.objects.filter(pk=m1.pk).update(created_at=now - timedelta(hours=1))

    # 2. Message sent 3h ago but delivered
    m2 = TransactionalMessage.objects.create(
        client=client_obj,
        contact=contact_obj,
        template=template_obj,
        email="m2@example.com",
        template_key="welcome",
        status=TransactionalMessageStatus.SENT,
        ses_message_id="ses-2",
        delivered_at=now - timedelta(hours=2, minutes=59),
    )
    TransactionalMessage.objects.filter(pk=m2.pk).update(created_at=now - timedelta(hours=3))

    # 3. Message sent 3h ago with bounce event
    m3 = TransactionalMessage.objects.create(
        client=client_obj,
        contact=contact_obj,
        template=template_obj,
        email="m3@example.com",
        template_key="welcome",
        status=TransactionalMessageStatus.SENT,
        last_error="soft_bounce",
        ses_message_id="ses-3",
    )
    TransactionalMessage.objects.filter(pk=m3.pk).update(created_at=now - timedelta(hours=3))
    EmailEvent.objects.create(
        transactional_message=m3,
        event_type=EmailEventType.BOUNCE,
        provider_event_id="prov-bounce-1",
    )

    # 4. Message sent 3h ago with complaint event
    m4 = TransactionalMessage.objects.create(
        client=client_obj,
        contact=contact_obj,
        template=template_obj,
        email="m4@example.com",
        template_key="welcome",
        status=TransactionalMessageStatus.COMPLAINED,
        ses_message_id="ses-4",
    )
    TransactionalMessage.objects.filter(pk=m4.pk).update(created_at=now - timedelta(hours=3))
    EmailEvent.objects.create(
        transactional_message=m4,
        event_type=EmailEventType.COMPLAINT,
        provider_event_id="prov-complaint-1",
    )

    alert = check_ses_outcome_gap(now, threshold=1)
    assert alert is None


@pytest.mark.django_db
def test_check_sns_persisted_gap_triggers_when_published_exceeds_persisted():
    published_at = timezone.now()
    for i in range(10):
        _sns_receipt(f"evt-{i}", published_at.isoformat())
    # Only 3 events persisted in Relay DB
    for i in range(3):
        EmailEvent.objects.create(
            event_type=EmailEventType.DELIVERED,
            provider_event_id=f"evt-{i}",
            metadata={"sns_topic_arn": "arn:aws:sns:eu-west-1:123:relay-production-ses-events",
                      "sns_timestamp": published_at.isoformat()},
        )

    now = timezone.now() + timedelta(minutes=3)

    # CloudWatch reports 10 published messages in the 30-min window
    fake_cw = MagicMock()
    fake_cw.get_metric_statistics.return_value = {
        "Datapoints": [{"Sum": 10.0}]
    }

    alert = check_sns_persisted_gap(now, cw_client=fake_cw)
    assert alert is not None
    assert alert["kind"] == "ses_sns_persisted_gap"
    assert alert["sns_published"] == 10
    assert alert["relay_persisted"] == 3


@pytest.mark.django_db
def test_check_sns_persisted_gap_silent_when_persisted_matches_or_exceeds():
    published_at = timezone.now()
    for notification_id in ["evt-match-0", "evt-match-1", "delay-0", "delay-1"]:
        _sns_receipt(notification_id, published_at.isoformat())
    # 2 EmailEvents + 2 OperatorAudits for SES
    for i in range(2):
        EmailEvent.objects.create(
            event_type=EmailEventType.DELIVERED,
            provider_event_id=f"evt-match-{i}",
            metadata={"sns_topic_arn": "arn:aws:sns:eu-west-1:123:relay-production-ses-events",
                      "sns_timestamp": published_at.isoformat()},
        )
    for i in range(2):
        OperatorAudit.objects.create(
            action="ses.delivery_delay.unsupported",
            target_type="ses_notification",
            target_id=0,
            metadata={"provider_event_id": f"delay-{i}",
                      "sns_timestamp": published_at.isoformat(),
                      "sns_topic_arn": "arn:aws:sns:eu-west-1:123:relay-production-ses-events"},
        )

    now = timezone.now() + timedelta(minutes=3)

    fake_cw = MagicMock()
    fake_cw.get_metric_statistics.return_value = {
        "Datapoints": [{"Sum": 4.0}]
    }

    alert = check_sns_persisted_gap(now, cw_client=fake_cw)
    assert alert is None


@pytest.mark.django_db
def test_check_ses_delivery_gap_sends_alert_and_throttles(client_obj, contact_obj, template_obj, monkeypatch):
    now = timezone.now()
    stuck = TransactionalMessage.objects.create(
        client=client_obj,
        contact=contact_obj,
        template=template_obj,
        email=contact_obj.email,
        template_key="welcome",
        status=TransactionalMessageStatus.SENT,
        ses_message_id="ses-stuck-2",
        delivered_at=None,
    )
    TransactionalMessage.objects.filter(pk=stuck.pk).update(created_at=now - timedelta(hours=3))

    sent_payloads = []

    def fake_send(payload, *, test=False):
        sent_payloads.append(payload)
        return "msg-id-123"

    monkeypatch.setattr("mailing.services.ses_monitor.send_monitor_alert", fake_send)

    # First check triggers alert
    alerts = check_ses_delivery_gap(now=now, force=True)
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "ses_outcome_gap"
    assert len(sent_payloads) == 1

    # Second check without force throttled
    alerts2 = check_ses_delivery_gap(now=now + timedelta(minutes=10))
    assert len(alerts2) == 0
    assert len(sent_payloads) == 1


@pytest.mark.django_db
def test_management_command_check_ses_delivery_gap(monkeypatch):
    calls = []

    def fake_send(payload, *, test=False):
        calls.append((payload, test))
        return "test-msg-id"

    monkeypatch.setattr("mailing.management.commands.check_ses_delivery_gap.send_monitor_alert", fake_send)

    call_command("check_ses_delivery_gap", "--test-alert")
    assert len(calls) == 1
    assert calls[0][1] is True


@pytest.mark.django_db
def test_gap_receipt_survives_restart_and_force_but_new_send_alerts(client_obj, contact_obj, template_obj, monkeypatch):
    now = timezone.now()
    message = TransactionalMessage.objects.create(
        client=client_obj, contact=contact_obj, template=template_obj, email=contact_obj.email,
        template_key="welcome", status=TransactionalMessageStatus.SENT, ses_message_id="first-send",
        sent_at=now - timedelta(hours=3),
    )
    sent = []
    monkeypatch.setattr("mailing.services.ses_monitor.send_monitor_alert", lambda payload, **kw: sent.append(payload))
    monkeypatch.setattr("mailing.services.ses_monitor.check_sns_persisted_gap", lambda *a, **kw: None)
    assert len(check_ses_delivery_gap(now=now)) == 1
    monkeypatch.setattr("mailing.services.ses_monitor._last_alert_by_kind", {})
    assert check_ses_delivery_gap(now=now + timedelta(hours=2), force=True) == []
    assert check_ses_outcome_gap(now) is None
    assert len(sent) == 1
    receipt = OperatorAudit.objects.get(action="monitor.ses_outcome_gap.reported", target_id=message.pk)
    assert receipt.metadata["ses_message_id"] == "first-send"
    # The same database row can be sent again by an explicitly authorized retry.
    TransactionalMessage.objects.filter(pk=message.pk).update(ses_message_id="second-send")
    assert len(check_ses_delivery_gap(now=now)) == 1
    assert len(sent) == 2


@pytest.mark.django_db
def test_gap_failed_transport_retries_and_new_message_is_not_throttled(client_obj, contact_obj, template_obj, monkeypatch):
    now = timezone.now()
    def create_message(send_id):
        return TransactionalMessage.objects.create(
            client=client_obj, contact=contact_obj, template=template_obj, email=contact_obj.email,
            template_key="welcome", status=TransactionalMessageStatus.SENT, ses_message_id=send_id,
            sent_at=now - timedelta(hours=3),
        )
    create_message("failed-transport")
    monkeypatch.setattr("mailing.services.ses_monitor.check_sns_persisted_gap", lambda *a, **kw: None)
    def fail(*a, **kw):
        raise RuntimeError("SMTP unavailable")
    monkeypatch.setattr("mailing.services.ses_monitor.send_monitor_alert", fail)
    assert check_ses_delivery_gap(now=now) == []
    assert not OperatorAudit.objects.filter(action="monitor.ses_outcome_gap.reported").exists()
    sent = []
    monkeypatch.setattr("mailing.services.ses_monitor.send_monitor_alert", lambda payload, **kw: sent.append(payload))
    assert len(check_ses_delivery_gap(now=now)) == 1
    second = create_message("new-gap")
    assert check_ses_delivery_gap(now=now)[0]["message_ids"] == [second.pk]
    assert len(sent) == 2


@pytest.mark.django_db
def test_gap_uses_send_time_and_requires_ses_acceptance(client_obj, contact_obj, template_obj):
    now = timezone.now()
    for send_id, sent_at in [("recent-send", now - timedelta(minutes=10)), ("", None)]:
        message = TransactionalMessage.objects.create(
            client=client_obj, contact=contact_obj, template=template_obj, email=contact_obj.email,
            template_key="welcome", status=TransactionalMessageStatus.SENT, ses_message_id=send_id, sent_at=sent_at,
        )
        TransactionalMessage.objects.filter(pk=message.pk).update(created_at=now - timedelta(days=2))
    assert check_ses_outcome_gap(now) is None


@pytest.mark.django_db
def test_gap_test_transport_does_not_acknowledge_real_incident(client_obj, contact_obj, template_obj, monkeypatch):
    now = timezone.now()
    TransactionalMessage.objects.create(
        client=client_obj, contact=contact_obj, template=template_obj, email=contact_obj.email,
        template_key="welcome", status=TransactionalMessageStatus.SENT, ses_message_id="test-only",
        sent_at=now - timedelta(hours=3),
    )
    monkeypatch.setattr("mailing.services.ses_monitor.send_monitor_alert", lambda *a, **kw: None)
    monkeypatch.setattr("mailing.services.ses_monitor.check_sns_persisted_gap", lambda *a, **kw: None)
    assert len(check_ses_delivery_gap(now=now, test=True)) == 1
    assert check_ses_outcome_gap(now) is not None


@pytest.mark.django_db
def test_sns_gap_cannot_be_hidden_by_local_or_other_topic_events():
    for i in range(2):
        _sns_receipt(f"lost-{i}", timezone.now().isoformat())
    for i, topic in enumerate(["", "arn:aws:sns:eu-west-1:123:other-topic"]):
        EmailEvent.objects.create(event_type=EmailEventType.DELIVERED, provider_event_id=f"other-{i}",
                                  metadata={"sns_topic_arn": topic})
    EmailEvent.objects.create(event_type=EmailEventType.QUEUED)
    fake_cw = MagicMock()
    fake_cw.get_metric_statistics.return_value = {"Datapoints": [{"Sum": 2.0}]}
    alert = check_sns_persisted_gap(timezone.now() + timedelta(minutes=3), cw_client=fake_cw)
    assert alert["relay_persisted"] == 0


@pytest.mark.django_db(transaction=True)
def test_concurrent_gap_checkers_send_only_once(client_obj, contact_obj, template_obj, monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locking required")
    now = timezone.now()
    TransactionalMessage.objects.create(
        client=client_obj, contact=contact_obj, template=template_obj, email=contact_obj.email,
        template_key="welcome", status=TransactionalMessageStatus.SENT, ses_message_id="concurrent",
        sent_at=now - timedelta(hours=3),
    )
    entered, release = Event(), Event()
    sent = []
    def send(payload, **kw):
        sent.append(payload)
        entered.set()
        assert release.wait(timeout=10)
    def check():
        close_old_connections()
        try:
            return check_ses_delivery_gap(now=now)
        finally:
            close_old_connections()
    monkeypatch.setattr("mailing.services.ses_monitor.send_monitor_alert", send)
    monkeypatch.setattr("mailing.services.ses_monitor.check_sns_persisted_gap", lambda *a, **kw: None)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(check)
        try:
            assert entered.wait(timeout=10)
            assert executor.submit(check).result(timeout=5) == []
        finally:
            release.set()
        assert len(first.result(timeout=5)) == 1
    assert len(sent) == 1
    assert check_ses_delivery_gap(now=now) == []


def _reset_sns_gap_state(monkeypatch):
    monkeypatch.setattr(ses_monitor_module, "_pending_sns_gap", None)
    monkeypatch.setattr(ses_monitor_module, "_last_alert_by_kind", {})


@pytest.mark.django_db
def test_sns_gap_requires_confirmation_before_paging(monkeypatch):
    now = timezone.now()
    _reset_sns_gap_state(monkeypatch)
    gap = {
        "kind": "ses_sns_persisted_gap",
        "window_minutes": 30,
        "sns_published": 17,
        "relay_persisted": 16,
        "topic": "relay-production-ses-events",
    }
    monkeypatch.setattr(ses_monitor_module, "check_sns_persisted_gap", lambda *a, **kw: gap)
    monkeypatch.setattr(ses_monitor_module, "report_ses_outcome_gap", lambda *a, **kw: None)
    sent = []
    monkeypatch.setattr(ses_monitor_module, "send_monitor_alert", lambda payload, **kw: sent.append(payload))

    # First sighting starts pending, no page (normal ingest lag window).
    assert check_ses_delivery_gap(now=now) == []
    assert len(sent) == 0
    assert ses_monitor_module._pending_sns_gap is not None

    # Still gap one minute later: confirming, still no page.
    assert check_ses_delivery_gap(now=now + timedelta(minutes=1)) == []
    assert len(sent) == 0

    # Gap persists past the 5-minute confirmation: pages once.
    alerts = check_ses_delivery_gap(now=now + timedelta(minutes=6))
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "ses_sns_persisted_gap"
    assert len(sent) == 1

    # Immediate re-check throttled (1h), no duplicate page.
    assert check_ses_delivery_gap(now=now + timedelta(minutes=7)) == []
    assert len(sent) == 1


@pytest.mark.django_db
def test_sns_gap_resolved_before_confirmation_never_pages(monkeypatch):
    now = timezone.now()
    _reset_sns_gap_state(monkeypatch)
    gap = {
        "kind": "ses_sns_persisted_gap",
        "window_minutes": 30,
        "sns_published": 17,
        "relay_persisted": 16,
        "topic": "relay-production-ses-events",
    }
    calls = {"gap": True}
    monkeypatch.setattr(
        ses_monitor_module, "check_sns_persisted_gap", lambda *a, **kw: gap if calls["gap"] else None
    )
    monkeypatch.setattr(ses_monitor_module, "report_ses_outcome_gap", lambda *a, **kw: None)
    sent = []
    monkeypatch.setattr(ses_monitor_module, "send_monitor_alert", lambda payload, **kw: sent.append(payload))

    assert check_ses_delivery_gap(now=now) == []
    assert ses_monitor_module._pending_sns_gap is not None

    # Lagging persist lands before confirmation: pending clears, never pages.
    calls["gap"] = False
    assert check_ses_delivery_gap(now=now + timedelta(minutes=2)) == []
    assert len(sent) == 0
    assert ses_monitor_module._pending_sns_gap is None

    # A new isolated blip requires fresh confirmation, not an immediate page.
    calls["gap"] = True
    assert check_ses_delivery_gap(now=now + timedelta(minutes=3)) == []
    assert len(sent) == 0


@pytest.mark.django_db
def test_sns_gap_force_bypasses_confirmation(monkeypatch):
    now = timezone.now()
    _reset_sns_gap_state(monkeypatch)
    gap = {
        "kind": "ses_sns_persisted_gap",
        "window_minutes": 30,
        "sns_published": 17,
        "relay_persisted": 16,
        "topic": "relay-production-ses-events",
    }
    monkeypatch.setattr(ses_monitor_module, "check_sns_persisted_gap", lambda *a, **kw: gap)
    monkeypatch.setattr(ses_monitor_module, "report_ses_outcome_gap", lambda *a, **kw: None)
    sent = []
    monkeypatch.setattr(ses_monitor_module, "send_monitor_alert", lambda payload, **kw: sent.append(payload))

    alerts = check_ses_delivery_gap(now=now, force=True)
    assert len(alerts) == 1
    assert len(sent) == 1


@pytest.mark.django_db
def test_outcome_gap_ignores_active_delivery_delay(client_obj, contact_obj, template_obj):
    now = timezone.now()
    message = TransactionalMessage.objects.create(
        client=client_obj, contact=contact_obj, template=template_obj, email=contact_obj.email,
        template_key="welcome", status=TransactionalMessageStatus.SENT, ses_message_id="delay-active",
        sent_at=now - timedelta(hours=3),
    )
    OperatorAudit.objects.create(
        action="ses.delivery_delay.unsupported",
        target_type="transactionalmessage",
        target_id=message.pk,
        metadata={
            "ses_message_id": "delay-active",
            "expiration_time": (now + timedelta(hours=11)).isoformat(),
        },
    )
    assert check_ses_outcome_gap(now) is None


@pytest.mark.django_db
def test_outcome_gap_fires_after_delivery_delay_expiration(client_obj, contact_obj, template_obj):
    now = timezone.now()
    message = TransactionalMessage.objects.create(
        client=client_obj, contact=contact_obj, template=template_obj, email=contact_obj.email,
        template_key="welcome", status=TransactionalMessageStatus.SENT, ses_message_id="delay-expired",
        sent_at=now - timedelta(hours=16),
    )
    OperatorAudit.objects.create(
        action="ses.delivery_delay.unsupported",
        target_type="transactionalmessage",
        target_id=message.pk,
        metadata={
            "ses_message_id": "delay-expired",
            "expiration_time": (now - timedelta(minutes=1)).isoformat(),
        },
    )
    alert = check_ses_outcome_gap(now)
    assert alert is not None
    assert message.pk in alert["message_ids"]


@pytest.mark.django_db
def test_outcome_gap_delay_without_expiration_uses_retry_window(client_obj, contact_obj, template_obj):
    now = timezone.now()
    recent = TransactionalMessage.objects.create(
        client=client_obj, contact=contact_obj, template=template_obj, email=contact_obj.email,
        template_key="welcome", status=TransactionalMessageStatus.SENT, ses_message_id="delay-recent",
        sent_at=now - timedelta(hours=3),
    )
    OperatorAudit.objects.create(
        action="ses.delivery_delay.unsupported",
        target_type="transactionalmessage",
        target_id=recent.pk,
        metadata={"ses_message_id": "delay-recent"},
    )
    assert check_ses_outcome_gap(now) is None

    old = TransactionalMessage.objects.create(
        client=client_obj, contact=contact_obj, template=template_obj, email="old@example.com",
        template_key="welcome", status=TransactionalMessageStatus.SENT, ses_message_id="delay-old",
        sent_at=now - timedelta(hours=20),
    )
    audit = OperatorAudit.objects.create(
        action="ses.delivery_delay.unsupported",
        target_type="transactionalmessage",
        target_id=old.pk,
        metadata={"ses_message_id": "delay-old"},
    )
    OperatorAudit.objects.filter(pk=audit.pk).update(created_at=now - timedelta(hours=15))
    alert = check_ses_outcome_gap(now)
    assert alert is not None
    assert old.pk in alert["message_ids"]
    assert recent.pk not in alert["message_ids"]


SNS_TOPIC = 'arn:aws:sns:eu-west-1:123:relay-production-ses-events'


def _sns_receipt(notification_id, published_at, *, status="SUCCESSFUL", topic=SNS_TOPIC):
    now = timezone.now()
    return DBTaskResult.objects.create(
        task_path="mailing.tasks.process_ses_webhook_event", queue_name="provider-events", backend_name="default",
        status=status, run_after=now, exception_class_path="", traceback="",
        args_kwargs={"args": [{"provider_event_id": notification_id,
                               "metadata": {"sns_topic_arn": topic, "sns_timestamp": published_at}}], "kwargs": {}},
    )


def _sns_event(notification_id, published_at, *, created_at=None, topic=SNS_TOPIC):
    _sns_receipt(notification_id, published_at, topic=topic)
    row = EmailEvent.objects.create(
        event_type=EmailEventType.DELIVERED,
        provider_event_id=notification_id,
        metadata={'sns_topic_arn': topic, 'sns_message_id': notification_id, 'sns_timestamp': published_at},
    )
    if created_at is not None:
        EmailEvent.objects.filter(pk=row.pk).update(created_at=created_at)
    return row


def _sns_cw(count):
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {'Datapoints': [{'Sum': float(count)}]}
    return cw


@pytest.mark.django_db
def test_sns_publication_cohort_counts_seven_boundary_straddling_events():
    now = timezone.now().replace(second=0, microsecond=0)
    end = now - timedelta(minutes=2)
    for i in range(7):
        _sns_event(f'late-{i}', (end - timedelta(milliseconds=100+i)).isoformat(),
                   created_at=end + timedelta(milliseconds=390+i*50))
    assert check_sns_persisted_gap(now, cw_client=_sns_cw(7)) is None


@pytest.mark.django_db
def test_sns_publication_cohort_deduplicates_events_and_audits_without_masking_loss():
    now = timezone.now().replace(second=0, microsecond=0)
    end = now - timedelta(minutes=2)
    stamp = (end - timedelta(seconds=1)).isoformat()
    _sns_receipt('lost-id', stamp)
    _sns_event('same-id', stamp)
    for _ in range(2):
        OperatorAudit.objects.create(action='ses.delivery_delay.unsupported', target_type='ses_notification',
                                     target_id=0, metadata={'sns_topic_arn': SNS_TOPIC,
                                                           'sns_timestamp': stamp,
                                                           'provider_event_id': 'same-id'})
    alert = check_sns_persisted_gap(now, cw_client=_sns_cw(2))
    assert alert['relay_persisted'] == 1


@pytest.mark.django_db
def test_sns_publication_cohort_includes_delay_audit_persisted_after_window():
    now = timezone.now().replace(second=0, microsecond=0)
    end = now - timedelta(minutes=2)
    _sns_receipt('delay-id', (end-timedelta(seconds=1)).isoformat())
    OperatorAudit.objects.create(action='ses.delivery_delay.unsupported', target_type='ses_notification',
                                 target_id=0, metadata={'sns_topic_arn': SNS_TOPIC,
                                                       'sns_timestamp': (end-timedelta(seconds=1)).isoformat(),
                                                       'sns_message_id': 'delay-id'})
    assert check_sns_persisted_gap(now, cw_client=_sns_cw(1)) is None


@pytest.mark.django_db
@pytest.mark.parametrize('stamp', [None, '', 'invalid', '2026-99-99T25:99:99Z', 123, '2026-10-25T09:00:00'])
def test_sns_missing_or_invalid_publication_evidence_never_fills_gap(stamp):
    now = timezone.now().replace(second=0, microsecond=0)
    _sns_event('unknown-time', stamp)
    _sns_receipt('known-missing-id', (now-timedelta(minutes=3)).isoformat())
    assert check_sns_persisted_gap(now, cw_client=_sns_cw(1))['relay_persisted'] == 0


@pytest.mark.django_db
def test_sns_publication_window_half_open_utc_boundaries_and_topic_scope():
    now = timezone.now().replace(second=0, microsecond=0)
    end = now - timedelta(minutes=2)
    start = end - timedelta(minutes=30)
    # Offset strings and Z describe the same instants as CloudWatch UTC times.
    _sns_event('start', start.isoformat().replace('+00:00', 'Z'))
    _sns_event('offset', (end-timedelta(seconds=1)).astimezone(
        timezone.get_fixed_timezone(120)).isoformat())
    _sns_event('end', end.isoformat())
    _sns_event('before', (start-timedelta(milliseconds=1)).isoformat())
    _sns_event('other-topic', start.isoformat(), topic=SNS_TOPIC+'-other')
    alert = check_sns_persisted_gap(now, cw_client=_sns_cw(3))
    assert alert is None


@pytest.mark.django_db
def test_sns_confirmation_rechecks_original_cohort_after_loss_ages_out(monkeypatch):
    now = timezone.now().replace(second=0, microsecond=0)
    _reset_sns_gap_state(monkeypatch)
    monkeypatch.setattr(ses_monitor_module, 'report_ses_outcome_gap', lambda *a, **kw: None)
    sent = []
    monkeypatch.setattr(ses_monitor_module, 'send_monitor_alert', lambda payload, **kw: sent.append(payload))
    cw = _sns_cw(1)
    _sns_receipt('lost-id', (now-timedelta(minutes=3)).isoformat())
    assert check_ses_delivery_gap(now=now, cw_client=cw) == []
    first_query = cw.get_metric_statistics.call_args.kwargs
    # Even a long scheduler pause must not let loss roll out of the window.
    _sns_event('new-unrelated-traffic', (now+timedelta(minutes=35)).isoformat())
    alerts = check_ses_delivery_gap(now=now+timedelta(minutes=40), cw_client=cw)
    assert cw.get_metric_statistics.call_args.kwargs == first_query
    assert alerts[0]['relay_persisted'] == 0
    assert alerts[0]['window_end'] == (now-timedelta(minutes=2)).isoformat()
    assert len(sent) == 1


@pytest.mark.django_db
def test_sns_late_persist_resolves_fixed_cohort_before_confirmation(monkeypatch):
    now = timezone.now().replace(second=0, microsecond=0)
    _reset_sns_gap_state(monkeypatch)
    monkeypatch.setattr(ses_monitor_module, 'report_ses_outcome_gap', lambda *a, **kw: None)
    sent = []
    monkeypatch.setattr(ses_monitor_module, 'send_monitor_alert', lambda payload, **kw: sent.append(payload))
    cw = _sns_cw(1)
    _sns_receipt('late-cohort-id', (now-timedelta(minutes=2, milliseconds=500)).isoformat())
    assert check_ses_delivery_gap(now=now, cw_client=cw) == []
    _sns_event('late-cohort-id', (now-timedelta(minutes=2, milliseconds=500)).isoformat())
    assert check_ses_delivery_gap(now=now+timedelta(minutes=6), cw_client=cw) == []
    assert ses_monitor_module._pending_sns_gap is None
    assert sent == []


@pytest.mark.django_db
def test_sns_cloudwatch_failure_does_not_hide_exact_missing_id(monkeypatch):
    now = timezone.now().replace(second=0, microsecond=0)
    _reset_sns_gap_state(monkeypatch)
    monkeypatch.setattr(ses_monitor_module, 'report_ses_outcome_gap', lambda *a, **kw: None)
    monkeypatch.setattr(ses_monitor_module, 'send_monitor_alert', lambda *a, **kw: None)
    cw = _sns_cw(1)
    _sns_receipt('lost-id', (now-timedelta(minutes=3)).isoformat())
    assert check_ses_delivery_gap(now=now, cw_client=cw) == []
    cw.get_metric_statistics.side_effect = RuntimeError('unavailable')
    alerts = check_ses_delivery_gap(now=now+timedelta(minutes=6), cw_client=cw)
    assert alerts[0]['missing_notification_ids'] == ['lost-id']
    assert alerts[0]['sns_published'] is None


@pytest.mark.django_db
def test_sns_clock_crossing_at_start_never_pages_complete_receipt_cohort(monkeypatch):
    """Real recurrence: envelope 09:36:59.973, queue 09:37:00.014, CW counts next minute."""
    now = timezone.now().replace(second=0, microsecond=0)
    start = now-timedelta(minutes=32)
    _sns_event('boundary-id', (start-timedelta(milliseconds=27)).isoformat(),
               created_at=start+timedelta(milliseconds=795))
    _sns_event('inside-id', (start+timedelta(seconds=1)).isoformat())
    _reset_sns_gap_state(monkeypatch)
    monkeypatch.setattr(ses_monitor_module, 'report_ses_outcome_gap', lambda *a, **kw: None)
    sent = []
    monkeypatch.setattr(ses_monitor_module, 'send_monitor_alert', lambda payload, **kw: sent.append(payload))
    # Published=2 vs timestamp-cohort=1 is not missing data: all exact IDs persisted.
    for minutes in [0, 1, 6, 7]:
        assert check_ses_delivery_gap(now=now+timedelta(minutes=minutes), cw_client=_sns_cw(2)) == []
    assert sent == []


@pytest.mark.django_db
def test_exact_missing_id_pages_even_if_unrelated_rows_and_metric_counts_balance(monkeypatch):
    now = timezone.now().replace(second=0, microsecond=0)
    stamp = (now-timedelta(minutes=3)).isoformat()
    _sns_receipt('actually-lost', stamp, status='FAILED')
    _sns_event('unrelated', stamp)
    _reset_sns_gap_state(monkeypatch)
    monkeypatch.setattr(ses_monitor_module, 'report_ses_outcome_gap', lambda *a, **kw: None)
    sent = []
    monkeypatch.setattr(ses_monitor_module, 'send_monitor_alert', lambda payload, **kw: sent.append(payload))
    monkeypatch.setenv('RELAY_IMAGE_TAG', 'test-image')
    monkeypatch.setenv('HOSTNAME', 'production-scheduler')
    cw = _sns_cw(1)  # Total persisted equals published; exact loss must still page.
    assert check_ses_delivery_gap(now=now, cw_client=cw) == []
    alert = check_ses_delivery_gap(now=now+timedelta(minutes=6), cw_client=cw)[0]
    assert alert['missing_notification_ids'] == ['actually-lost']
    assert alert['missing_count'] == 1
    assert alert['comparison_basis'] == 'durable_ingress_sns_ids'
    assert alert['image_tag'] == 'test-image'
    assert alert['monitor_component'] == 'production-scheduler'


@pytest.mark.django_db
def test_duplicate_ingress_receipts_cannot_create_loss_count():
    now = timezone.now().replace(second=0, microsecond=0)
    stamp = (now-timedelta(minutes=3)).isoformat()
    _sns_receipt('same-id', stamp)
    _sns_event('same-id', stamp)
    assert check_sns_persisted_gap(now, cw_client=_sns_cw(2)) is None


def _transport_sqs(*, source_depth=0, dlq_depth=0):
    sqs = MagicMock()
    source = {'ApproximateNumberOfMessages': str(source_depth), 'ApproximateNumberOfMessagesNotVisible': '0',
              'RedrivePolicy': '{"deadLetterTargetArn":"arn:aws:sqs:eu-west-1:123:ses-dlq"}'}
    dlq = {'ApproximateNumberOfMessages': str(dlq_depth), 'ApproximateNumberOfMessagesNotVisible': '0'}
    sqs.get_queue_attributes.side_effect = [{'Attributes': source}, {'Attributes': dlq}]
    return sqs


def _transport_cw(*, failures=0, age=0):
    cw = MagicMock()
    cw.get_metric_statistics.side_effect = lambda **kw: {'Datapoints': [
        {'Maximum': age} if kw['MetricName']=='ApproximateAgeOfOldestMessage' else {'Sum': failures}]}
    return cw


def test_transport_pages_sns_terminal_loss_without_ingress_receipt(settings):
    settings.SQS_SES_WEBHOOKS_QUEUE_URL = 'https://sqs.eu-west-1.amazonaws.com/123/ses-source'
    alerts = check_ses_transport_health(timezone.now(), cw_client=_transport_cw(failures=1),
                                        sqs_client=_transport_sqs())
    assert alerts[0]['kind'] == 'ses_sns_delivery_failed'
    assert alerts[0]['failed_notifications'] == 1


def test_transport_pages_poison_message_in_dlq_without_provider_receipt(settings):
    settings.SQS_SES_WEBHOOKS_QUEUE_URL = 'https://sqs.eu-west-1.amazonaws.com/123/ses-source'
    alerts = check_ses_transport_health(timezone.now(), cw_client=_transport_cw(),
                                        sqs_client=_transport_sqs(dlq_depth=1))
    assert alerts[0]['kind'] == 'ses_sqs_dlq_nonempty'
    assert alerts[0]['depth'] == 1


@pytest.mark.parametrize('age,expected', [(299, []), (300, ['ses_sqs_ingress_stalled'])])
def test_transport_pages_stalled_ingress_but_allows_short_queue_latency(settings, age, expected):
    settings.SQS_SES_WEBHOOKS_QUEUE_URL = 'https://sqs.eu-west-1.amazonaws.com/123/ses-source'
    alerts = check_ses_transport_health(timezone.now(), cw_client=_transport_cw(age=age),
                                        sqs_client=_transport_sqs(source_depth=1))
    assert [a['kind'] for a in alerts] == expected


def test_transport_query_failure_is_logged_not_reported_as_success(settings, caplog):
    settings.SQS_SES_WEBHOOKS_QUEUE_URL = 'https://sqs.eu-west-1.amazonaws.com/123/ses-source'
    sqs=MagicMock()
    sqs.get_queue_attributes.side_effect = RuntimeError('private details')
    assert check_ses_transport_health(timezone.now(), cw_client=_transport_cw(), sqs_client=sqs) == []
    assert 'ses_monitor_transport_query_failed' in caplog.text
    assert 'private details' not in caplog.text


@pytest.mark.django_db
def test_metric_outage_cannot_discard_independently_proven_dlq_fault(monkeypatch):
    _reset_sns_gap_state(monkeypatch)
    monkeypatch.setattr(ses_monitor_module, 'report_ses_outcome_gap', lambda *a, **kw: None)
    monkeypatch.setattr(ses_monitor_module, 'check_ses_transport_health',
                        lambda *a, **kw: [{'kind': 'ses_sqs_dlq_nonempty', 'depth': 1}])
    cw = _sns_cw(0)
    cw.get_metric_statistics.side_effect = RuntimeError('unavailable')
    sent = []
    monkeypatch.setattr(ses_monitor_module, 'send_monitor_alert', lambda payload, **kw: sent.append(payload))
    alerts = check_ses_delivery_gap(now=timezone.now(), cw_client=cw)
    assert [a['kind'] for a in alerts] == ['ses_sqs_dlq_nonempty']
    assert len(sent) == 1
