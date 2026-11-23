"""Admin message endpoints expose SES bounce diagnostics for suppression questions."""

import json

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client as HttpClient
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

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
from mailing.services.admin_auth import create_admin_api_key
from mailing.services.ses_webhooks import MAX_DIAGNOSTIC_CODE_LENGTH, build_ses_metadata
from mailing.sqs import records_from_messages
from mailing.workers import ses_webhooks_handler

pytestmark = pytest.mark.django_db

DIAG = "smtp; 550 5.1.1 The email account that you tried to reach does not exist"


@pytest.fixture
def app_client():
    org = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    return Client.objects.create(organization=org, name="DTC Courses", slug="dtc-courses")


@pytest.fixture
def template(app_client):
    return EmailTemplate.objects.create(
        client=app_client, key="password-reset", name="Password reset", subject="Reset", is_transactional=True
    )


def make_message(app_client, template, n, status=TransactionalMessageStatus.SENT):
    contact = Contact.objects.create(email=f"person{n}@example.com", verified_at=timezone.now())
    return TransactionalMessage.objects.create(
        client=app_client,
        contact=contact,
        email=contact.email,
        template=template,
        template_key=template.key,
        status=status,
        idempotency_key=f"tx-{n}",
        subject="Reset",
        ses_message_id=f"ses-tx-{n}",
        sent_at=timezone.now(),
    )


@pytest.fixture
def http():
    user = get_user_model().objects.create_user("op", "op@example.com", "pw", is_staff=True)
    _, token = create_admin_api_key(user=user, name="api")
    return HttpClient(HTTP_AUTHORIZATION=f"Bearer {token}")


def bounce_sns(message, *, diagnostic, sns_id="sns-bounce-diag"):
    ses = {
        "eventType": "Bounce",
        "mail": {
            "timestamp": "2026-11-23T12:00:00Z",
            "source": "courses@example.com",
            "messageId": message.ses_message_id,
            "destination": [message.email],
        },
        "bounce": {
            "timestamp": "2026-11-23T12:00:02Z",
            "bounceType": "Permanent",
            "bounceSubType": "General",
            "bouncedRecipients": [
                {"emailAddress": message.email, "action": "failed", "status": "5.1.1", "diagnosticCode": diagnostic}
            ],
        },
    }
    return {
        "Type": "Notification",
        "MessageId": sns_id,
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:ses",
        "Message": json.dumps(ses),
        "Timestamp": "2026-11-23T12:00:03Z",
        "SignatureVersion": "1",
        "Signature": "datamailer-local-sns-signature",
        "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-test.pem",
    }


def ingest(payload):
    event = records_from_messages([{"MessageId": "m1", "ReceiptHandle": "r1", "Body": json.dumps(payload)}])
    assert ses_webhooks_handler(event, None) == {"batchItemFailures": []}


def test_bounce_ingest_stores_redacted_diagnostic_code(app_client, template):
    message = make_message(app_client, template, 1)
    ingest(bounce_sns(message, diagnostic=f"{DIAG} <{message.email}>"))

    event = EmailEvent.objects.get(transactional_message=message, event_type=EmailEventType.BOUNCE)
    assert event.metadata["diagnostic_code"].startswith(DIAG)
    assert message.email not in event.metadata["diagnostic_code"]
    assert "[redacted-email]" in event.metadata["diagnostic_code"]
    assert event.metadata["bounce_status"] == "5.1.1"
    assert event.metadata["bounce_action"] == "failed"


def test_bounce_metadata_caps_diagnostic_length_and_tolerates_missing_recipients():
    long = build_ses_metadata(
        {"eventType": "Bounce", "bounce": {"bounceType": "Transient", "bouncedRecipients": [{"diagnosticCode": "x" * 5000}]}},
        {},
    )
    assert len(long["diagnostic_code"]) == MAX_DIAGNOSTIC_CODE_LENGTH
    bare = build_ses_metadata({"eventType": "Bounce", "bounce": {"bounceType": "Permanent"}}, {})
    assert "diagnostic_code" not in bare
    odd = build_ses_metadata({"eventType": "Bounce", "bounce": {"bouncedRecipients": ["not-a-dict"]}}, {})
    assert "diagnostic_code" not in odd


def test_admin_message_detail_and_list_expose_bounce_diagnostic(http, app_client, template):
    bounced = make_message(app_client, template, 1)
    make_message(app_client, template, 2)
    ingest(bounce_sns(bounced, diagnostic=DIAG))

    detail = http.get(f"/api/admin/transactional-messages/{bounced.id}").json()
    assert detail["status"] == TransactionalMessageStatus.BOUNCED
    assert detail["bounce"]["diagnostic_code"] == DIAG
    assert detail["bounce"]["bounce_type"] == "Permanent"
    assert detail["bounce"]["bounce_sub_type"] == "General"
    assert detail["bounce"]["bounce_status"] == "5.1.1"
    assert detail["bounce"]["occurred_at"] == "2026-11-23T12:00:02Z"

    items = {item["id"]: item for item in http.get("/api/admin/transactional-messages").json()["items"]}
    assert items[bounced.id]["bounce"]["diagnostic_code"] == DIAG
    assert len(items) == 2
    assert [item["bounce"] for item in items.values() if item["id"] != bounced.id] == [None]


def test_admin_message_list_filters_by_status(http, app_client, template):
    bounced = make_message(app_client, template, 1, status=TransactionalMessageStatus.BOUNCED)
    make_message(app_client, template, 2)

    body = http.get("/api/admin/transactional-messages", {"status": "bounced"}).json()
    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [bounced.id]
    # Pre-capture bounces have no event yet: the field is present and null.
    assert body["items"][0]["bounce"] is None

    bad = http.get("/api/admin/transactional-messages", {"status": "nope"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_filter"


def test_admin_message_list_bounce_lookup_is_not_n_plus_one(http, app_client, template):
    for n in range(6):
        ingest(bounce_sns(make_message(app_client, template, n), diagnostic=DIAG, sns_id=f"sns-{n}"))

    with CaptureQueriesContext(connection) as small:
        http.get("/api/admin/transactional-messages", {"limit": 2})
    with CaptureQueriesContext(connection) as large:
        http.get("/api/admin/transactional-messages", {"limit": 6})
    assert len(large.captured_queries) == len(small.captured_queries)
