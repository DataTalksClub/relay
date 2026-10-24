from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from django.core.management import call_command
from django.utils import timezone

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
    # Only 3 events persisted in Relay DB
    for i in range(3):
        EmailEvent.objects.create(
            event_type=EmailEventType.DELIVERED,
            provider_event_id=f"evt-{i}",
        )

    now = timezone.now() + timedelta(seconds=1)

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
    # 2 EmailEvents + 2 OperatorAudits for SES
    for i in range(2):
        EmailEvent.objects.create(
            event_type=EmailEventType.DELIVERED,
            provider_event_id=f"evt-match-{i}",
        )
    for i in range(2):
        OperatorAudit.objects.create(
            action="ses.delivery_delay.unsupported",
            target_type="ses_notification",
            target_id=0,
            metadata={"provider_event_id": f"delay-{i}"},
        )

    now = timezone.now() + timedelta(seconds=1)

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
