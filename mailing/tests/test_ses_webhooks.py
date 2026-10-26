import base64
import http.client
import json
import urllib.error
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    Client,
    ClientCallback,
    ClientCallbackStatus,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    OperatorAudit,
    Organization,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services import ses_webhooks as ses_webhooks_service
from mailing.services.client_callbacks import process_due_client_callbacks
from mailing.services.contacts import is_marketing_email_allowed, is_transactional_email_allowed
from mailing.services.ses_webhooks import SNS_MOCK_SIGNATURE, canonical_sns_message
from mailing.sqs import records_from_messages
from mailing.tests.callback_helpers import create_callback_endpoint, install_fake_opener
from mailing.workers import ses_webhooks_handler

pytestmark = pytest.mark.django_db


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="DataTalksClub", slug="datatalks-club")


@pytest.fixture
def app_client(organization):
    return Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")


@pytest.fixture
def contact():
    return Contact.objects.create(email="person@example.com", verified_at=timezone.now())


@pytest.fixture
def campaign(audience, app_client):
    return Campaign.objects.create(audience=audience, client=app_client, subject="Weekly update")


@pytest.fixture
def recipient(campaign, contact):
    return CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email=contact.email,
        status=CampaignRecipientStatus.SENT,
        ses_message_id="ses-campaign-1",
        sent_at=timezone.now(),
    )


@pytest.fixture
def transactional_message(app_client, contact):
    template = EmailTemplate.objects.create(
        client=app_client,
        key="password-reset",
        name="Password reset",
        subject="Reset",
        is_transactional=True,
    )
    return TransactionalMessage.objects.create(
        client=app_client,
        contact=contact,
        email=contact.email,
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.SENT,
        idempotency_key="tx-1",
        subject="Reset",
        ses_message_id="ses-tx-1",
        sent_at=timezone.now(),
    )


def test_webhook_endpoint_enqueues_valid_notification_without_mutating_state(client, recipient, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.ses_webhooks.enqueue_ses_webhook", lambda payload: enqueued.append(payload))

    response = client.post(
        reverse("mailing:ses_webhook"),
        data=json.dumps(sns_payload(ses_payload("Delivery", "ses-campaign-1"), message_id="sns-delivery-1")),
        content_type="application/json",
    )

    recipient.refresh_from_db()
    assert response.status_code == 200
    assert response.json()["enqueued"] is True
    assert recipient.delivered_at is None
    assert EmailEvent.objects.count() == 0
    assert enqueued == [
        {
            "contract": "ses-webhooks",
            "version": 1,
            "provider": "ses",
            "provider_event_id": "sns-delivery-1",
            "notification_type": "delivery",
            "received_at": enqueued[0]["received_at"],
            "ses_message_id": "ses-campaign-1",
            "mail_message_id": "ses-campaign-1",
            "metadata": {
                "sns_message_id": "sns-delivery-1",
                "sns_topic_arn": "arn:aws:sns:us-east-1:123456789012:ses",
                "sns_timestamp": "2026-05-24T12:00:01Z",
                "mail_timestamp": "2026-05-24T12:00:00Z",
                "mail_source": "newsletter@example.com",
            },
        }
    ]


def test_webhook_endpoint_rejects_invalid_signature_without_enqueue_or_mutation(client, recipient, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.ses_webhooks.enqueue_ses_webhook", lambda payload: enqueued.append(payload))
    payload = sns_payload(ses_payload("Delivery", "ses-campaign-1"), signature="wrong")

    response = client.post(reverse("mailing:ses_webhook"), data=json.dumps(payload), content_type="application/json")

    recipient.refresh_from_db()
    assert response.status_code == 403
    assert enqueued == []
    assert recipient.delivered_at is None
    assert EmailEvent.objects.count() == 0


def test_webhook_endpoint_handles_subscription_confirmation_only_when_allowed(client, monkeypatch):
    calls = []

    class FakeResponse:
        def __enter__(self):
            calls.append("confirmed")
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr("mailing.services.ses_webhooks.urlopen", lambda url, timeout: FakeResponse())
    payload = sns_payload({}, message_type="SubscriptionConfirmation") | {
        "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&TopicArn=arn:aws:sns:us-east-1:123456789012:ses",
        "Token": "token-1",
    }

    skipped = client.post(reverse("mailing:ses_webhook"), data=json.dumps(payload), content_type="application/json")

    with override_settings(
        SES_WEBHOOKS_ALLOW_SUBSCRIPTION_CONFIRMATION=True,
        SES_WEBHOOKS_ALLOWED_TOPIC_ARNS=("arn:aws:sns:us-east-1:123456789012:ses",),
    ):
        confirmed = client.post(
            reverse("mailing:ses_webhook"), data=json.dumps(payload), content_type="application/json"
        )

    assert skipped.status_code == 200
    assert skipped.json()["confirmed"] is False
    assert confirmed.status_code == 200
    assert confirmed.json()["confirmed"] is True
    assert calls == ["confirmed"]


@override_settings(SES_WEBHOOKS_ALLOW_SUBSCRIPTION_CONFIRMATION=True, SES_WEBHOOKS_ALLOWED_TOPIC_ARNS=())
def test_subscription_confirmation_refused_without_topic_allowlist(client):
    payload = sns_payload({}, message_type="SubscriptionConfirmation") | {
        "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&TopicArn=arn:aws:sns:us-east-1:123456789012:ses",
        "Token": "token-1",
    }

    response = client.post(reverse("mailing:ses_webhook"), data=json.dumps(payload), content_type="application/json")

    assert response.status_code == 403


@override_settings(SES_WEBHOOKS_SIGNATURE_MODE="strict")
def test_strict_sns_signature_validation_uses_certificate_rules(client, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = build_test_certificate(key)
    payload = sns_payload(ses_payload("Delivery", "ses-unknown"), signature="") | {
        "SignatureVersion": "2",
        "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-test.pem",
    }
    signature = key.sign(canonical_sns_message(payload).encode(), padding.PKCS1v15(), hashes.SHA256())
    payload["Signature"] = base64.b64encode(signature).decode()
    enqueued = []
    monkeypatch.setattr("mailing.services.ses_webhooks.fetch_signing_certificate", lambda url: cert)
    monkeypatch.setattr("mailing.services.ses_webhooks.enqueue_ses_webhook", lambda payload: enqueued.append(payload))

    response = client.post(reverse("mailing:ses_webhook"), data=json.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert enqueued[0]["provider_event_id"] == payload["MessageId"]


def test_webhook_endpoint_rejects_malformed_and_unsupported_payloads(client):
    malformed = client.post(reverse("mailing:ses_webhook"), data="{bad-json", content_type="application/json")
    unsupported = client.post(
        reverse("mailing:ses_webhook"),
        data=json.dumps(sns_payload({}, message_type="UnsubscribeConfirmation")),
        content_type="application/json",
    )

    assert malformed.status_code == 400
    assert unsupported.status_code == 400
    assert EmailEvent.objects.count() == 0


def test_worker_delivery_updates_campaign_recipient_and_stats_idempotently(recipient):
    payload = webhook_payload("delivery", "sns-delivery-1", "ses-campaign-1")
    event = records_from_payloads([("first", payload), ("duplicate", payload)])

    response = ses_webhooks_handler(event, None)

    recipient.refresh_from_db()
    recipient.campaign.refresh_from_db()
    assert response == {"batchItemFailures": []}
    assert recipient.delivered_at is not None
    assert recipient.status == CampaignRecipientStatus.SENT
    assert recipient.campaign.delivered_count == 1
    assert EmailEvent.objects.filter(event_type=EmailEventType.DELIVERED, campaign_recipient=recipient).count() == 1


def test_worker_raw_sns_delivery_updates_campaign_recipient(recipient):
    event = records_from_payloads(
        [("message-1", sns_payload(ses_payload("Delivery", "ses-campaign-1"), message_id="sns-raw-delivery"))]
    )

    response = ses_webhooks_handler(event, None)

    recipient.refresh_from_db()
    recipient.campaign.refresh_from_db()
    event = EmailEvent.objects.get()
    assert response == {"batchItemFailures": []}
    assert recipient.delivered_at is not None
    assert recipient.campaign.delivered_count == 1
    assert event.event_type == EmailEventType.DELIVERED
    assert event.provider_event_id == "sns-raw-delivery"
    assert event.metadata["sns_message_id"] == "sns-raw-delivery"
    assert event.metadata["sns_timestamp"] == "2026-05-24T12:00:01Z"


def test_worker_preserves_sqs_timing_metadata(recipient):
    event = records_from_payloads([
        ("message-1", sns_payload(ses_payload("Delivery", "ses-campaign-1")))
    ])
    event["Records"][0]["attributes"] = {
        "SentTimestamp": "1792756799000",
        "ApproximateFirstReceiveTimestamp": "1792756800000",
        "ApproximateReceiveCount": "2",
    }

    assert ses_webhooks_handler(event, None) == {"batchItemFailures": []}

    metadata = EmailEvent.objects.get().metadata
    assert metadata["sqs_sent_timestamp"] == "1792756799000"
    assert metadata["sqs_approximate_first_receive_timestamp"] == "1792756800000"
    assert "ApproximateReceiveCount" not in metadata


def test_worker_delivery_updates_transactional_message(transactional_message):
    response = ses_webhooks_handler(
        records_from_payloads([("message-1", webhook_payload("delivery", "sns-tx-delivery", "ses-tx-1"))]),
        None,
    )

    transactional_message.refresh_from_db()
    assert response == {"batchItemFailures": []}
    assert transactional_message.delivered_at is not None
    assert transactional_message.status == TransactionalMessageStatus.SENT
    assert (
        EmailEvent.objects.filter(
            event_type=EmailEventType.DELIVERED,
            transactional_message=transactional_message,
        ).count()
        == 1
    )


def test_worker_campaign_delivery_emits_no_client_callback(recipient, app_client):
    create_callback_endpoint(app_client)

    response = ses_webhooks_handler(
        records_from_payloads([("message-1", webhook_payload("delivery", "sns-cmp-delivery", "ses-campaign-1"))]),
        None,
    )

    assert response == {"batchItemFailures": []}
    # Campaign-recipient transitions stay on the CMP channel; the client
    # callback contract carries transactional deliveries only.
    assert ClientCallback.objects.count() == 0


def test_worker_transactional_delivery_emits_client_callback(transactional_message, app_client):
    create_callback_endpoint(app_client)

    response = ses_webhooks_handler(
        records_from_payloads([("message-1", webhook_payload("delivery", "sns-tx-cb-delivery", "ses-tx-1"))]),
        None,
    )

    assert response == {"batchItemFailures": []}
    callback = ClientCallback.objects.get()
    assert callback.status == ClientCallbackStatus.PENDING
    assert callback.event_type == "delivery.delivered"
    assert callback.payload["event_type"] == "delivery.delivered"
    assert callback.payload["message_id"] == str(transactional_message.pk)
    assert callback.payload["client_reference"] == "tx-1"
    assert transactional_message.contact.normalized_email not in callback.body


def test_worker_hard_bounce_suppresses_campaign_contact(recipient, audience, app_client):
    payload = webhook_payload(
        "bounce",
        "sns-bounce-1",
        "ses-campaign-1",
        metadata={"bounce_type": "Permanent", "bounce_sub_type": "General"},
    )

    ses_webhooks_handler(records_from_payloads([("message-1", payload)]), None)

    recipient.refresh_from_db()
    recipient.contact.refresh_from_db()
    recipient.campaign.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.BOUNCED
    assert recipient.contact.hard_bounced_at is not None
    assert recipient.campaign.bounce_count == 1
    assert is_marketing_email_allowed(recipient.contact, audience, app_client) is False
    assert is_transactional_email_allowed(recipient.contact) is False
    assert EmailEvent.objects.filter(event_type=EmailEventType.BOUNCE, campaign_recipient=recipient).count() == 1


def test_worker_hard_bounce_emits_signed_client_callback(transactional_message, app_client, monkeypatch):
    endpoint = create_callback_endpoint(app_client)
    opener = install_fake_opener(monkeypatch)
    payload = webhook_payload(
        "bounce",
        "sns-bounce-cmp",
        "ses-tx-1",
        metadata={"bounce_type": "Permanent", "bounce_sub_type": "General"},
    )

    ses_webhooks_handler(records_from_payloads([("message-1", payload)]), None)
    assert ClientCallback.objects.filter(status=ClientCallbackStatus.PENDING).count() == 1
    process_due_client_callbacks()

    assert len(opener.requests) == 1
    request = opener.requests[0]
    assert request["url"] == endpoint.url
    assert "authorization" not in request["headers"]
    assert request["headers"]["x-relay-signature"].startswith("sha256=")
    assert request["headers"]["x-relay-event-type"] == "delivery.bounced"
    body = json.loads(request["body"].decode("utf-8"))
    assert body["event_type"] == "delivery.bounced"
    assert body["bounce_type"] == "hard"
    assert body["reason_code"] == "hard_bounce"
    assert body["client_reference"] == "tx-1"
    assert transactional_message.contact.normalized_email not in request["body"].decode("utf-8")


def test_worker_campaign_hard_bounce_emits_no_client_callback(recipient, app_client):
    create_callback_endpoint(app_client)
    payload = webhook_payload(
        "bounce",
        "sns-bounce-cmp",
        "ses-campaign-1",
        metadata={"bounce_type": "Permanent", "bounce_sub_type": "General"},
    )

    ses_webhooks_handler(records_from_payloads([("message-1", payload)]), None)

    assert ClientCallback.objects.count() == 0


def test_worker_raw_sns_hard_bounce_suppresses_campaign_contact(recipient, audience, app_client):
    event = records_from_payloads(
        [
            (
                "message-1",
                sns_payload(
                    ses_payload(
                        "Bounce",
                        "ses-campaign-1",
                        detail={"bounceType": "Permanent", "bounceSubType": "General"},
                    ),
                    message_id="sns-raw-bounce",
                ),
            )
        ]
    )

    response = ses_webhooks_handler(event, None)

    recipient.refresh_from_db()
    recipient.contact.refresh_from_db()
    recipient.campaign.refresh_from_db()
    assert response == {"batchItemFailures": []}
    assert recipient.status == CampaignRecipientStatus.BOUNCED
    assert recipient.contact.hard_bounced_at is not None
    assert recipient.campaign.bounce_count == 1
    assert is_marketing_email_allowed(recipient.contact, audience, app_client) is False
    assert is_transactional_email_allowed(recipient.contact) is False
    assert EmailEvent.objects.filter(event_type=EmailEventType.BOUNCE, campaign_recipient=recipient).count() == 1


def test_worker_complaint_suppresses_transactional_contact(transactional_message):
    payload = webhook_payload("complaint", "sns-complaint-1", "ses-tx-1")

    ses_webhooks_handler(records_from_payloads([("message-1", payload)]), None)

    transactional_message.refresh_from_db()
    transactional_message.contact.refresh_from_db()
    assert transactional_message.status == TransactionalMessageStatus.COMPLAINED
    assert transactional_message.contact.complained_at is not None
    assert is_transactional_email_allowed(transactional_message.contact) is False
    assert (
        EmailEvent.objects.filter(
            event_type=EmailEventType.COMPLAINT,
            transactional_message=transactional_message,
        ).count()
        == 1
    )


def test_worker_raw_sns_complaint_suppresses_transactional_contact(transactional_message):
    event = records_from_payloads(
        [
            (
                "message-1",
                sns_payload(
                    ses_payload("Complaint", "ses-tx-1", detail={"complaintFeedbackType": "abuse"}),
                    message_id="sns-raw-complaint",
                ),
            )
        ]
    )

    response = ses_webhooks_handler(event, None)

    transactional_message.refresh_from_db()
    transactional_message.contact.refresh_from_db()
    assert response == {"batchItemFailures": []}
    assert transactional_message.status == TransactionalMessageStatus.COMPLAINED
    assert transactional_message.contact.complained_at is not None
    assert is_transactional_email_allowed(transactional_message.contact) is False
    assert (
        EmailEvent.objects.filter(
            event_type=EmailEventType.COMPLAINT,
            transactional_message=transactional_message,
        ).count()
        == 1
    )


def test_worker_soft_bounce_audits_without_hard_suppression(recipient):
    payload = webhook_payload(
        "bounce",
        "sns-soft-bounce-1",
        "ses-campaign-1",
        metadata={"bounce_type": "Transient", "bounce_sub_type": "MailboxFull"},
    )

    ses_webhooks_handler(records_from_payloads([("message-1", payload)]), None)

    recipient.refresh_from_db()
    recipient.contact.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.SENT
    assert recipient.last_error == "soft_bounce"
    assert recipient.contact.hard_bounced_at is None
    assert EmailEvent.objects.filter(event_type=EmailEventType.BOUNCE, campaign_recipient=recipient).count() == 1


def test_worker_open_and_click_update_summary_fields_without_duplicate_unique_counts(recipient):
    payloads = [
        ("open-1", webhook_payload("open", "sns-open-1", "ses-campaign-1")),
        ("open-dup", webhook_payload("open", "sns-open-1", "ses-campaign-1")),
        ("click-1", webhook_payload("click", "sns-click-1", "ses-campaign-1", metadata={"url": "https://example.com"})),
    ]

    ses_webhooks_handler(records_from_payloads(payloads), None)

    recipient.refresh_from_db()
    recipient.campaign.refresh_from_db()
    assert recipient.open_count == 1
    assert recipient.click_count == 1
    assert recipient.first_opened_at is not None
    assert recipient.first_clicked_at is not None
    assert recipient.campaign.open_count == 1
    assert recipient.campaign.unique_open_count == 1
    assert recipient.campaign.click_count == 1
    assert recipient.campaign.unique_click_count == 1
    assert EmailEvent.objects.filter(campaign_recipient=recipient).count() == 2


def test_worker_uncorrelated_message_id_records_diagnostics_without_suppression(contact):
    payload = webhook_payload("bounce", "sns-unknown-bounce", "missing-ses-message")

    response = ses_webhooks_handler(records_from_payloads([("message-1", payload)]), None)

    contact.refresh_from_db()
    event = EmailEvent.objects.get()
    assert response == {"batchItemFailures": []}
    assert event.campaign_recipient_id is None
    assert event.transactional_message_id is None
    assert event.metadata["ses_message_id"] == "missing-ses-message"
    assert contact.hard_bounced_at is None


def test_worker_returns_partial_batch_failures_for_bad_records(recipient):
    event = records_from_payloads(
        [
            ("valid", webhook_payload("delivery", "sns-delivery-2", "ses-campaign-1")),
            ("invalid", webhook_payload("delivery", "sns-bad", "ses-campaign-1") | {"version": 999}),
        ]
    )

    response = ses_webhooks_handler(event, None)

    recipient.refresh_from_db()
    assert response == {"batchItemFailures": [{"itemIdentifier": "invalid"}]}
    assert recipient.delivered_at is not None


def test_worker_raw_sns_invalid_envelope_fails_only_that_batch_item(recipient):
    valid = sns_payload(ses_payload("Delivery", "ses-campaign-1"), message_id="sns-raw-delivery-2")
    invalid = sns_payload(ses_payload("Delivery", "ses-campaign-1"), message_id="sns-raw-invalid") | {
        "Message": "{not-json",
    }
    event = records_from_payloads([("valid", valid), ("invalid", invalid)])

    response = ses_webhooks_handler(event, None)

    recipient.refresh_from_db()
    assert response == {"batchItemFailures": [{"itemIdentifier": "invalid"}]}
    assert recipient.delivered_at is not None
    assert EmailEvent.objects.filter(provider_event_id="sns-raw-delivery-2").count() == 1
    assert EmailEvent.objects.filter(provider_event_id="sns-raw-invalid").count() == 0


def sns_payload(message, *, message_id="sns-message-1", message_type="Notification", signature=SNS_MOCK_SIGNATURE):
    return {
        "Type": message_type,
        "MessageId": message_id,
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:ses",
        "Message": json.dumps(message),
        "Timestamp": "2026-05-24T12:00:01Z",
        "SignatureVersion": "1",
        "Signature": signature,
        "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-test.pem",
    }


def ses_payload(event_type, ses_message_id, *, detail=None):
    return {
        "eventType": event_type,
        "mail": {
            "timestamp": "2026-05-24T12:00:00Z",
            "source": "newsletter@example.com",
            "messageId": ses_message_id,
        },
        event_type.casefold(): detail or {},
    }


def webhook_payload(notification_type, provider_event_id, ses_message_id, *, metadata=None):
    return {
        "contract": "ses-webhooks",
        "version": 1,
        "provider": "ses",
        "provider_event_id": provider_event_id,
        "notification_type": notification_type,
        "received_at": "2026-05-24T12:00:01Z",
        "ses_message_id": ses_message_id,
        "metadata": {"mail_timestamp": "2026-05-24T12:00:00Z"} | (metadata or {}),
    }


def records_from_payloads(message_payloads):
    return records_from_messages(
        [
            {
                "MessageId": message_id,
                "ReceiptHandle": f"{message_id}-receipt",
                "Body": json.dumps(payload),
            }
            for message_id, payload in message_payloads
        ]
    )


def build_test_certificate(private_key):
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.us-east-1.amazonaws.com")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(dt_timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(dt_timezone.utc) + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def test_delivery_delay_creates_operator_audit_for_transactional_message(transactional_message):
    message_payload = {
        "eventType": "DeliveryDelay",
        "mail": {
            "timestamp": "2026-10-24T10:38:18.805Z",
            "source": "newsletter@example.com",
            "messageId": transactional_message.ses_message_id,
            "destination": [transactional_message.email],
        },
        "deliveryDelay": {
            "timestamp": "2026-10-24T10:38:18.805Z",
            "delayType": "TransientCommunicationFailure",
            "expirationTime": "2026-10-24T18:07:05.177Z",
            "delayedRecipients": [
                {
                    "emailAddress": transactional_message.email,
                    "status": "4.4.1",
                    "diagnosticCode": "smtp; 421 4.4.1 Unable to connect to remote host",
                }
            ],
        },
    }
    event = records_from_payloads([("msg-tx-delay", sns_payload(message_payload, message_id="sns-tx-delay-1"))])

    response = ses_webhooks_handler(event, None)

    assert response == {"batchItemFailures": []}
    transactional_message.refresh_from_db()
    assert transactional_message.status == TransactionalMessageStatus.SENT
    assert EmailEvent.objects.count() == 0

    audit_entry = OperatorAudit.objects.get(metadata__provider_event_id="sns-tx-delay-1")
    assert audit_entry.action == "ses.delivery_delay.unsupported"
    assert audit_entry.target_type == "transactionalmessage"
    assert audit_entry.target_id == transactional_message.id
    assert audit_entry.metadata["delay_type"] == "TransientCommunicationFailure"
    assert audit_entry.metadata["diagnostic_code"] == "smtp; 421 4.4.1 Unable to connect to remote host"
    assert audit_entry.metadata["delayed_status"] == "4.4.1"
    assert transactional_message.email not in str(audit_entry.metadata)


def test_delivery_delay_creates_operator_audit_for_campaign_recipient(recipient):
    message_payload = {
        "eventType": "DeliveryDelay",
        "mail": {
            "timestamp": "2026-10-24T10:38:18.805Z",
            "source": "newsletter@example.com",
            "messageId": recipient.ses_message_id,
            "destination": [recipient.email],
        },
        "deliveryDelay": {
            "timestamp": "2026-10-24T10:38:18.805Z",
            "delayType": "TransientCommunicationFailure",
            "delayedRecipients": [
                {
                    "emailAddress": recipient.email,
                    "status": "4.4.1",
                    "diagnosticCode": "smtp; 421 4.4.1 Unable to connect to remote host",
                }
            ],
        },
    }
    event = records_from_payloads([("msg-cr-delay", sns_payload(message_payload, message_id="sns-cr-delay-1"))])

    response = ses_webhooks_handler(event, None)

    assert response == {"batchItemFailures": []}
    recipient.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.SENT
    assert EmailEvent.objects.count() == 0

    audit_entry = OperatorAudit.objects.get(metadata__provider_event_id="sns-cr-delay-1")
    assert audit_entry.action == "ses.delivery_delay.unsupported"
    assert audit_entry.target_type == "campaignrecipient"
    assert audit_entry.target_id == recipient.id
    assert recipient.email not in str(audit_entry.metadata)


def test_delivery_delay_idempotency_does_not_duplicate_operator_audit(transactional_message):
    message_payload = {
        "eventType": "DeliveryDelay",
        "mail": {
            "timestamp": "2026-10-24T10:38:18.805Z",
            "source": "newsletter@example.com",
            "messageId": transactional_message.ses_message_id,
            "destination": [transactional_message.email],
        },
        "deliveryDelay": {
            "timestamp": "2026-10-24T10:38:18.805Z",
            "delayType": "TransientCommunicationFailure",
        },
    }
    event = records_from_payloads([("msg-tx-delay", sns_payload(message_payload, message_id="sns-tx-delay-idem"))])

    response1 = ses_webhooks_handler(event, None)
    response2 = ses_webhooks_handler(event, None)

    assert response1 == {"batchItemFailures": []}
    assert response2 == {"batchItemFailures": []}
    assert OperatorAudit.objects.filter(metadata__provider_event_id="sns-tx-delay-idem").count() == 1


def test_delivery_delay_redacts_email_in_diagnostic_code(transactional_message):
    message_payload = {
        "eventType": "DeliveryDelay",
        "mail": {
            "source": "newsletter@example.com",
            "messageId": transactional_message.ses_message_id,
        },
        "deliveryDelay": {
            "delayType": "TransientCommunicationFailure",
            "delayedRecipients": [
                {
                    "emailAddress": transactional_message.email,
                    "diagnosticCode": f"smtp; error connecting to mailbox {transactional_message.email}",
                }
            ],
        },
    }
    event = records_from_payloads([("msg-tx-delay", sns_payload(message_payload, message_id="sns-tx-delay-diag"))])

    response = ses_webhooks_handler(event, None)

    assert response == {"batchItemFailures": []}
    audit_entry = OperatorAudit.objects.get(metadata__provider_event_id="sns-tx-delay-diag")
    assert "[redacted-email]" in audit_entry.metadata["diagnostic_code"]
    assert transactional_message.email not in audit_entry.metadata["diagnostic_code"]


def test_unsupported_future_ses_event_type_creates_audit_entry():
    message_payload = {
        "eventType": "SomeFutureEvent",
        "mail": {
            "timestamp": "2026-10-24T10:38:18.805Z",
            "source": "newsletter@example.com",
            "messageId": "unknown-ses-id",
        },
    }
    event = records_from_payloads([("msg-unknown", sns_payload(message_payload, message_id="sns-unknown-1"))])

    response = ses_webhooks_handler(event, None)

    assert response == {"batchItemFailures": []}
    audit_entry = OperatorAudit.objects.get(metadata__provider_event_id="sns-unknown-1")
    assert audit_entry.action == "ses.event.unsupported"
    assert audit_entry.target_type == "ses_notification"
    assert audit_entry.target_id == 0
    assert audit_entry.metadata["event_type"] == "SomeFutureEvent"


def test_transactional_transient_bounce_is_terminal_without_contact_suppression(transactional_message):
    payload = webhook_payload(
        "bounce", "sns-retry-expired", "ses-tx-1",
        metadata={"bounce_type": "Transient", "bounce_sub_type": "General"},
    )
    event = records_from_payloads([("retry-expired", payload)])
    assert ses_webhooks_handler(event, None) == {"batchItemFailures": []}
    assert ses_webhooks_handler(event, None) == {"batchItemFailures": []}
    transactional_message.refresh_from_db()
    transactional_message.contact.refresh_from_db()
    assert transactional_message.status == TransactionalMessageStatus.BOUNCED
    assert transactional_message.last_error == "soft_bounce"
    assert transactional_message.contact.hard_bounced_at is None
    assert transactional_message.events.filter(provider_event_id="sns-retry-expired").count() == 1


@override_settings(SES_WEBHOOKS_SIGNATURE_MODE="strict")
def test_http_fallback_caches_signing_certificate(client, monkeypatch):
    ses_webhooks_service.clear_signing_certificate_cache()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert_bytes = build_test_certificate(key)
    fetch_calls = []

    class FakeResponse:
        def __enter__(self):
            fetch_calls.append(1)
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, limit=None):
            return cert_bytes

    monkeypatch.setattr(ses_webhooks_service, "urlopen", lambda url, timeout: FakeResponse())
    enqueued = []
    monkeypatch.setattr(ses_webhooks_service, "enqueue_ses_webhook", lambda payload: enqueued.append(payload))

    first = sns_payload(ses_payload("Delivery", "ses-cache-1"), message_id="sns-cache-1") | {
        "SignatureVersion": "2",
        "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-cache.pem",
    }
    signature = key.sign(
        canonical_sns_message(first).encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    first["Signature"] = base64.b64encode(signature).decode()
    second = dict(first) | {"MessageId": "sns-cache-2"}
    signature2 = key.sign(
        canonical_sns_message(second).encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    second["Signature"] = base64.b64encode(signature2).decode()

    try:
        response1 = client.post(reverse("mailing:ses_webhook"), data=json.dumps(first), content_type="application/json")
        response2 = client.post(reverse("mailing:ses_webhook"), data=json.dumps(second), content_type="application/json")
    finally:
        ses_webhooks_service.clear_signing_certificate_cache()

    assert response1.status_code == 200
    assert response2.status_code == 200
    assert len(enqueued) == 2
    assert len(fetch_calls) == 1, "second request with the same cert URL must not fetch again"


@override_settings(
    SES_WEBHOOKS_SIGNATURE_MODE="strict",
    SES_WEBHOOKS_ALLOWED_TOPIC_ARNS=("arn:aws:sns:us-east-1:123456789012:allowed",),
)
def test_http_fallback_rejects_unknown_topic_arn(client, monkeypatch):
    ses_webhooks_service.clear_signing_certificate_cache()
    fetch_calls = []
    monkeypatch.setattr(
        ses_webhooks_service, "urlopen", lambda url, timeout: (_ for _ in ()).throw(AssertionError("must not fetch"))
    )
    payload = sns_payload(ses_payload("Delivery", "ses-unknown-topic"), message_id="sns-topic-1")

    response = client.post(reverse("mailing:ses_webhook"), data=json.dumps(payload), content_type="application/json")

    ses_webhooks_service.clear_signing_certificate_cache()
    assert response.status_code == 403
    assert fetch_calls == []


@override_settings(SES_WEBHOOKS_SIGNATURE_MODE="strict", SES_WEBHOOKS_ALLOWED_TOPIC_ARNS=())
def test_http_fallback_empty_topic_allowlist_allows_any_topic(client, monkeypatch):
    ses_webhooks_service.clear_signing_certificate_cache()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert_bytes = build_test_certificate(key)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, limit=None):
            return cert_bytes

    monkeypatch.setattr(ses_webhooks_service, "urlopen", lambda url, timeout: FakeResponse())
    enqueued = []
    monkeypatch.setattr(ses_webhooks_service, "enqueue_ses_webhook", lambda payload: enqueued.append(payload))
    payload = sns_payload(ses_payload("Delivery", "ses-any-topic"), message_id="sns-any-topic") | {
        "SignatureVersion": "2",
        "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-any.pem",
    }
    signature = key.sign(canonical_sns_message(payload).encode(), padding.PKCS1v15(), hashes.SHA256())
    payload["Signature"] = base64.b64encode(signature).decode()

    try:
        response = client.post(reverse("mailing:ses_webhook"), data=json.dumps(payload), content_type="application/json")
    finally:
        ses_webhooks_service.clear_signing_certificate_cache()

    assert response.status_code == 200
    assert len(enqueued) == 1


@override_settings(SES_WEBHOOKS_SIGNATURE_MODE="strict", SES_WEBHOOKS_MAX_BODY_BYTES=64)
def test_http_fallback_rejects_oversized_body_before_signature_work(client, monkeypatch):
    monkeypatch.setattr(
        ses_webhooks_service, "urlopen", lambda url, timeout: (_ for _ in ()).throw(AssertionError("must not fetch"))
    )
    payload = sns_payload(ses_payload("Delivery", "ses-big-1"), message_id="sns-big-1")
    assert len(json.dumps(payload).encode()) > 64

    response = client.post(reverse("mailing:ses_webhook"), data=json.dumps(payload), content_type="application/json")

    assert response.status_code == 413


@override_settings(SES_WEBHOOKS_SIGNATURE_MODE="strict")
def test_http_fallback_rejects_missing_content_length(client):
    payload = sns_payload(ses_payload("Delivery", "ses-nocl-1"), message_id="sns-nocl-1")

    response = client.generic(
        "POST",
        reverse("mailing:ses_webhook"),
        data=json.dumps(payload),
        content_type="application/json",
        CONTENT_LENGTH="",
    )

    assert response.status_code == 413


# F3: per-type SES event times must stamp *_at fields, not the send time.
# Mail (send) time in the fixtures below is 2026-05-24T12:00:00Z; the event
# time is 2026-05-24T12:05:00Z. Before the fix, delivered/first_opened_at
# were stamped with the mail time because build_ses_metadata never set
# event_timestamp.

F3_SEND_TIME = "2026-05-24T12:00:00Z"
F3_EVENT_TIME = "2026-05-24T12:05:00Z"


def ses_payload_with_event_time(event_type, ses_message_id, *, detail_extra=None):
    detail = {"timestamp": F3_EVENT_TIME}
    detail.update(detail_extra or {})
    payload = ses_payload(event_type, ses_message_id, detail=detail)
    payload["mail"]["timestamp"] = F3_SEND_TIME
    return payload


def test_f3_delivery_stamps_event_time_not_send_time(recipient):
    event = records_from_payloads(
        [("message-1", sns_payload(ses_payload_with_event_time("Delivery", "ses-campaign-1"), message_id="sns-f3-delivery"))]
    )

    assert ses_webhooks_handler(event, None) == {"batchItemFailures": []}

    recipient.refresh_from_db()
    assert recipient.delivered_at == timezone.datetime(2026, 5, 24, 12, 5, tzinfo=dt_timezone.utc)
    metadata = EmailEvent.objects.get().metadata
    assert metadata["event_timestamp"] == F3_EVENT_TIME
    assert metadata["mail_timestamp"] == F3_SEND_TIME


def test_f3_open_and_click_stamp_event_times(recipient):
    payloads = [
        ("open-1", sns_payload(ses_payload_with_event_time("Open", "ses-campaign-1"), message_id="sns-f3-open")),
        (
            "click-1",
            sns_payload(
                ses_payload_with_event_time("Click", "ses-campaign-1", detail_extra={"link": "https://example.com"}),
                message_id="sns-f3-click",
            ),
        ),
    ]

    assert ses_webhooks_handler(records_from_payloads(payloads), None) == {"batchItemFailures": []}

    recipient.refresh_from_db()
    assert recipient.first_opened_at == timezone.datetime(2026, 5, 24, 12, 5, tzinfo=dt_timezone.utc)
    assert recipient.first_clicked_at == timezone.datetime(2026, 5, 24, 12, 5, tzinfo=dt_timezone.utc)


def test_f3_hard_bounce_and_complaint_stamp_event_times(recipient, transactional_message):
    bounce = sns_payload(
        ses_payload_with_event_time(
            "Bounce", "ses-campaign-1", detail_extra={"bounceType": "Permanent", "bounceSubType": "General"}
        ),
        message_id="sns-f3-bounce",
    )
    complaint = sns_payload(
        ses_payload_with_event_time("Complaint", "ses-tx-1"),
        message_id="sns-f3-complaint",
    )

    assert ses_webhooks_handler(records_from_payloads([("b", bounce), ("c", complaint)]), None) == {
        "batchItemFailures": []
    }

    recipient.contact.refresh_from_db()
    transactional_message.contact.refresh_from_db()
    assert recipient.contact.hard_bounced_at == timezone.datetime(2026, 5, 24, 12, 5, tzinfo=dt_timezone.utc)
    assert transactional_message.contact.complained_at == timezone.datetime(
        2026, 5, 24, 12, 5, tzinfo=dt_timezone.utc
    )


def test_f3_transactional_delivery_stamps_event_time(transactional_message):
    event = records_from_payloads(
        [("message-1", sns_payload(ses_payload_with_event_time("Delivery", "ses-tx-1"), message_id="sns-f3-tx"))]
    )

    assert ses_webhooks_handler(event, None) == {"batchItemFailures": []}

    transactional_message.refresh_from_db()
    assert transactional_message.delivered_at == timezone.datetime(2026, 5, 24, 12, 5, tzinfo=dt_timezone.utc)


def test_f3_send_without_event_time_falls_back_to_mail_time(recipient):
    payload = ses_payload("Send", "ses-campaign-1")
    payload["mail"]["timestamp"] = F3_SEND_TIME
    event = records_from_payloads([("message-1", sns_payload(payload, message_id="sns-f3-send"))])

    assert ses_webhooks_handler(event, None) == {"batchItemFailures": []}

    metadata = EmailEvent.objects.get().metadata
    assert "event_timestamp" not in metadata
    assert metadata["mail_timestamp"] == F3_SEND_TIME


@override_settings(SES_WEBHOOKS_SIGNATURE_MODE="strict")
def test_http_fallback_rejects_cert_url_with_userinfo(client, monkeypatch):
    """netloc `sns.x@host` tricks must not pass the SNS host check."""
    fetch_calls = []
    monkeypatch.setattr(
        ses_webhooks_service, "urlopen", lambda url, timeout: fetch_calls.append(url) or (_ for _ in ()).throw(
            AssertionError("must not fetch")
        )
    )
    payload = sns_payload(ses_payload("Delivery", "ses-userinfo-1"), message_id="sns-userinfo-1")
    payload["SigningCertURL"] = "https://sns.us-east-1@sns.us-east-1.amazonaws.com.evil.example/cert.pem"

    response = client.post(reverse("mailing:ses_webhook"), data=json.dumps(payload), content_type="application/json")

    assert response.status_code == 403
    assert fetch_calls == []


@override_settings(SES_WEBHOOKS_SIGNATURE_MODE="strict")
def test_http_fallback_rejects_attacker_subdomain_cert_host(client, monkeypatch):
    """sns.<anything-with-dots>.amazonaws.com is not a real SNS cert host."""
    monkeypatch.setattr(
        ses_webhooks_service, "urlopen", lambda url, timeout: (_ for _ in ()).throw(AssertionError("must not fetch"))
    )
    payload = sns_payload(ses_payload("Delivery", "ses-sub-1"), message_id="sns-sub-1")
    payload["SigningCertURL"] = "https://sns.evil.acc.s3.us-east-1.amazonaws.com/cert.pem"

    response = client.post(reverse("mailing:ses_webhook"), data=json.dumps(payload), content_type="application/json")

    assert response.status_code == 403


@override_settings(SES_WEBHOOKS_SIGNATURE_MODE="strict")
def test_http_fallback_rejects_non_string_cert_url_without_500(client):
    payload = sns_payload(ses_payload("Delivery", "ses-nonstr-1"), message_id="sns-nonstr-1")
    payload["SigningCertURL"] = ["https://sns.us-east-1.amazonaws.com/cert.pem"]

    response = client.post(reverse("mailing:ses_webhook"), data=json.dumps(payload), content_type="application/json")

    assert response.status_code == 403


@override_settings(SES_WEBHOOKS_SIGNATURE_MODE="strict")
def test_http_fallback_cert_cache_expires_after_ttl(client, monkeypatch):
    ses_webhooks_service.clear_signing_certificate_cache()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert_bytes = build_test_certificate(key)
    fetch_calls = []

    class FakeResponse:
        def __enter__(self):
            fetch_calls.append(1)
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, limit=None):
            return cert_bytes

    monkeypatch.setattr(ses_webhooks_service, "urlopen", lambda url, timeout: FakeResponse())
    enqueued = []
    monkeypatch.setattr(ses_webhooks_service, "enqueue_ses_webhook", lambda payload: enqueued.append(payload))
    now = [1000.0]
    monkeypatch.setattr(ses_webhooks_service, "monotonic", lambda: now[0])

    def signed(member_id, cert_url):
        member = sns_payload(ses_payload("Delivery", "ses-ttl-1"), message_id=member_id) | {
            "SignatureVersion": "2",
            "SigningCertURL": cert_url,
        }
        signature = key.sign(
            canonical_sns_message(member).encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        member["Signature"] = base64.b64encode(signature).decode()
        return member

    cert_url = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-ttl.pem"
    try:
        first = client.post(
            reverse("mailing:ses_webhook"), data=json.dumps(signed("sns-ttl-1", cert_url)), content_type="application/json"
        )
        second = client.post(
            reverse("mailing:ses_webhook"),
            data=json.dumps(signed("sns-ttl-2", cert_url + "?x=1")),
            content_type="application/json",
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert len(fetch_calls) == 1, "query-string variants must share one cache entry"
        now[0] += 3600.0 + 1.0
        third = client.post(
            reverse("mailing:ses_webhook"), data=json.dumps(signed("sns-ttl-3", cert_url)), content_type="application/json"
        )
        assert third.status_code == 200
        assert len(fetch_calls) == 2, "expired entries must be refetched"
    finally:
        ses_webhooks_service.clear_signing_certificate_cache()


@override_settings(SES_WEBHOOKS_SIGNATURE_MODE="strict")
def test_http_fallback_rejects_oversized_certificate(client, monkeypatch):
    ses_webhooks_service.clear_signing_certificate_cache()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, limit=None):
            return b"x" * (ses_webhooks_service._MAX_CERT_BYTES + 1)

    monkeypatch.setattr(ses_webhooks_service, "urlopen", lambda url, timeout: FakeResponse())
    payload = sns_payload(ses_payload("Delivery", "ses-bigcert-1"), message_id="sns-bigcert-1") | {
        "SignatureVersion": "2",
        "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-big.pem",
    }
    payload["Signature"] = base64.b64encode(b"0" * 256).decode()

    try:
        response = client.post(reverse("mailing:ses_webhook"), data=json.dumps(payload), content_type="application/json")
    finally:
        ses_webhooks_service.clear_signing_certificate_cache()

    assert response.status_code == 403


def test_f3_delivery_delay_stamps_event_time(recipient):
    event_time = "2026-10-24T10:38:18.805Z"
    message_payload = {
        "eventType": "DeliveryDelay",
        "mail": {
            "timestamp": "2026-10-24T10:37:00.000Z",
            "source": "newsletter@example.com",
            "messageId": "ses-campaign-1",
        },
        "deliveryDelay": {
            "timestamp": event_time,
            "delayType": "TransientCommunicationFailure",
            "expirationTime": "2026-10-24T18:07:05.177Z",
            "delayedRecipients": [{"emailAddress": "x@example.com", "status": "4.4.1"}],
        },
    }
    event = records_from_payloads([("msg-delay", sns_payload(message_payload, message_id="sns-f3-delay"))])

    assert ses_webhooks_handler(event, None) == {"batchItemFailures": []}

    audit_entry = OperatorAudit.objects.get(metadata__provider_event_id="sns-f3-delay")
    assert audit_entry.metadata["event_timestamp"] == event_time


def test_f3_unparseable_event_time_falls_back_to_mail_time(recipient):
    payload = ses_payload_with_event_time("Delivery", "ses-campaign-1")
    payload["delivery"]["timestamp"] = "not-a-timestamp"
    event = records_from_payloads([("message-1", sns_payload(payload, message_id="sns-f3-badts"))])

    assert ses_webhooks_handler(event, None) == {"batchItemFailures": []}

    recipient.refresh_from_db()
    assert recipient.delivered_at == timezone.datetime(2026, 5, 24, 12, 0, tzinfo=dt_timezone.utc)


def test_f3_reject_falls_back_to_mail_time(recipient):
    payload = ses_payload("Reject", "ses-campaign-1", detail={"reason": "BadDomain"})
    payload["mail"]["timestamp"] = F3_SEND_TIME
    event = records_from_payloads([("message-1", sns_payload(payload, message_id="sns-f3-reject"))])

    assert ses_webhooks_handler(event, None) == {"batchItemFailures": []}

    event_row = EmailEvent.objects.get()
    assert "event_timestamp" not in event_row.metadata


@override_settings(SES_WEBHOOKS_SIGNATURE_MODE="strict")
def test_http_fallback_unreachable_cert_url_is_403_not_500(client, monkeypatch):
    def explode(url, timeout):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(ses_webhooks_service, "urlopen", explode)
    payload = sns_payload(ses_payload("Delivery", "ses-404cert-1"), message_id="sns-404cert-1") | {
        "SignatureVersion": "2",
        "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-missing.pem",
    }
    payload["Signature"] = base64.b64encode(b"0" * 256).decode()

    response = client.post(reverse("mailing:ses_webhook"), data=json.dumps(payload), content_type="application/json")

    assert response.status_code == 403


@override_settings(SES_WEBHOOKS_SIGNATURE_MODE="strict")
def test_http_fallback_mid_read_timeout_is_403_not_500(client, monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, limit=None):
            raise http.client.IncompleteRead(b"partial", 64 * 1024)

    monkeypatch.setattr(ses_webhooks_service, "urlopen", lambda url, timeout: FakeResponse())
    payload = sns_payload(ses_payload("Delivery", "ses-trunc-1"), message_id="sns-trunc-1") | {
        "SignatureVersion": "2",
        "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-trunc.pem",
    }
    payload["Signature"] = base64.b64encode(b"0" * 256).decode()

    response = client.post(reverse("mailing:ses_webhook"), data=json.dumps(payload), content_type="application/json")

    assert response.status_code == 403
