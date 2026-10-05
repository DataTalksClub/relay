"""RELAY_DRY_RUN pipeline tests: transactional skip semantics and campaign batches.

Offline only: the dry-run paths are driven with the default client factories
(no injected SES client) while spies fail the test on any boto3 construction
or STS call. No real AWS, no network, no LocalStack.
"""

import json
from unittest.mock import Mock

import pytest
from django.test import override_settings

from mailing.dry_run import DryRunSuppressed
from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    Organization,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.campaign_sender import CampaignSendResult, send_campaign_batch, send_campaign_test_message
from mailing.services.transactional import build_transactional_queue_payload
from mailing.sqs import records_from_messages
from mailing.workers import transactional_email_handler

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def api_client_record(organization):
    return Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")


@pytest.fixture
def contact():
    return Contact.objects.create(email="person@example.com")


@pytest.fixture
def template(api_client_record):
    return EmailTemplate.objects.create(
        client=api_client_record,
        key="email-verification",
        name="Email verification",
        subject="Mutable template subject",
        html_body="<p>Mutable template body</p>",
        text_body="Mutable template body",
    )


@pytest.fixture
def transactional_message(api_client_record, contact, template):
    return TransactionalMessage.objects.create(
        client=api_client_record,
        contact=contact,
        email=contact.normalized_email,
        from_email_id="courses",
        from_email="courses@dtcdev.click",
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.QUEUED,
        idempotency_key="verify-123",
        subject="Persisted subject",
        html_body="<p>Persisted body</p>",
        text_body="Persisted body",
    )


@pytest.fixture
def campaign_setup(organization):
    audience = Audience.objects.create(organization=organization, name="Newsletter", slug="newsletter")
    client = Client.objects.create(organization=organization, name="Datamailer", slug="datamailer")
    campaign = Campaign.objects.create(
        audience=audience,
        client=client,
        subject="Weekly update",
        html_body="<p>Hello</p>",
        text_body="Hello in text",
    )
    contact = Contact.objects.create(email="recipient@example.com")
    recipient = CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=contact.email)
    return campaign, recipient


@pytest.fixture
def released_campaign_setup(campaign_setup):
    # send_campaign_batch refuses DRAFT/SNAPSHOTTING campaigns, so release the
    # campaign first — the same QUEUED state queue_campaign leaves before dispatch
    # and the same released-campaign fixture shape the sender tests use.
    campaign, recipient = campaign_setup
    campaign.status = CampaignStatus.QUEUED
    campaign.save(update_fields=["status", "updated_at"])
    return campaign, recipient


def _forbid_boto3_and_sts(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("dry run must not construct boto3 clients or call STS")

    boto3_spy = Mock(side_effect=fail)
    sts_spy = Mock(side_effect=fail)
    monkeypatch.setattr("boto3.client", boto3_spy)
    monkeypatch.setattr("mailing.aws._assume_role", sts_spy)
    return boto3_spy, sts_spy


def _event(message_id, payload):
    return records_from_messages(
        [{"MessageId": message_id, "ReceiptHandle": f"{message_id}-receipt", "Body": json.dumps(payload)}]
    )


@override_settings(DEFAULT_FROM_EMAIL="sender@example.com", AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="")
def test_dry_run_transactional_send_skips_durably_and_acknowledges(transactional_message, monkeypatch):
    monkeypatch.setenv("RELAY_DRY_RUN", "1")
    _forbid_boto3_and_sts(monkeypatch)
    event = _event("message-1", build_transactional_queue_payload(transactional_message))

    first = transactional_email_handler(event)
    second = transactional_email_handler(event)

    transactional_message.refresh_from_db()
    assert first == {"batchItemFailures": []}
    assert second == {"batchItemFailures": []}
    assert transactional_message.status == TransactionalMessageStatus.SKIPPED
    assert transactional_message.ses_message_id == ""
    assert transactional_message.sent_at is None
    assert transactional_message.metadata["dry_run"] is True
    assert transactional_message.metadata["skip_reason"] == "dry_run"
    events = EmailEvent.objects.filter(transactional_message=transactional_message)
    assert events.count() == 1
    assert events.get().event_type == EmailEventType.SKIPPED
    assert events.get().metadata["reason"] == "dry_run"


@override_settings(DEFAULT_FROM_EMAIL="sender@example.com", AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="")
def test_unrecognized_flag_fails_transactional_send_closed(transactional_message, monkeypatch):
    monkeypatch.setenv("RELAY_DRY_RUN", "true")
    _forbid_boto3_and_sts(monkeypatch)
    event = _event("message-1", build_transactional_queue_payload(transactional_message))

    response = transactional_email_handler(event)

    transactional_message.refresh_from_db()
    assert response == {"batchItemFailures": [{"itemIdentifier": "message-1"}]}
    assert transactional_message.status == TransactionalMessageStatus.SENDING
    assert transactional_message.ses_message_id == ""
    assert EmailEvent.objects.count() == 0


@override_settings(
    DEFAULT_FROM_EMAIL="newsletter@example.com",
    PUBLIC_BASE_URL="https://mail.example.com",
)
def test_dry_run_campaign_batch_with_default_factory_lands_durable_failures(released_campaign_setup, monkeypatch):
    campaign, recipient = released_campaign_setup
    monkeypatch.setenv("RELAY_DRY_RUN", "1")
    boto3_spy, sts_spy = _forbid_boto3_and_sts(monkeypatch)

    result = send_campaign_batch(_campaign_payload(campaign, recipient))

    recipient.refresh_from_db()
    campaign.refresh_from_db()
    assert isinstance(result, CampaignSendResult)
    assert (result.sent_count, result.skipped_count, result.failed_count) == (0, 0, 1)
    assert recipient.status == CampaignRecipientStatus.FAILED
    assert "dry run" in recipient.last_error
    assert recipient.ses_message_id == ""
    assert campaign.sent_count == 0
    event = EmailEvent.objects.get(campaign_recipient=recipient)
    assert event.event_type == EmailEventType.FAILED
    assert "dry run" in event.metadata["error"]
    assert boto3_spy.call_count == 0
    assert sts_spy.call_count == 0


@override_settings(
    DEFAULT_FROM_EMAIL="newsletter@example.com",
    PUBLIC_BASE_URL="https://mail.example.com",
)
def test_unrecognized_flag_fails_campaign_batch_closed(released_campaign_setup, monkeypatch):
    campaign, recipient = released_campaign_setup
    monkeypatch.setenv("RELAY_DRY_RUN", "true")
    boto3_spy, sts_spy = _forbid_boto3_and_sts(monkeypatch)

    result = send_campaign_batch(_campaign_payload(campaign, recipient))

    recipient.refresh_from_db()
    assert result.failed_count == 1
    assert recipient.status == CampaignRecipientStatus.FAILED
    assert "RELAY_DRY_RUN" in recipient.last_error
    assert boto3_spy.call_count == 0
    assert sts_spy.call_count == 0


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com")
def test_dry_run_campaign_test_message_raises_loudly(campaign_setup, monkeypatch):
    campaign, _ = campaign_setup
    monkeypatch.setenv("RELAY_DRY_RUN", "1")
    _forbid_boto3_and_sts(monkeypatch)

    with pytest.raises(DryRunSuppressed):
        send_campaign_test_message(campaign, "staff@example.com")


def _campaign_payload(campaign, recipient):
    return {
        "contract": "campaign-email",
        "version": 1,
        "campaign_id": campaign.id,
        "batch_id": f"campaign-{campaign.id}-batch-1",
        "campaign_recipient_ids": [recipient.id],
    }
