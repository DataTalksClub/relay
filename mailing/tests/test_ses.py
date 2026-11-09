from email import message_from_bytes

import boto3
import pytest
from botocore.stub import Stubber
from django.test import override_settings

from mailing.ses import send_email, send_raw_email


class FakeSesClient:
    def __init__(self):
        self.raw_params = None

    def send_raw_email(self, **params):
        self.raw_params = params
        return {"MessageId": "raw-message-123"}


@override_settings(AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="", SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND=0)
def test_send_email_uses_expected_ses_payload():
    client = boto3.client(
        "ses",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "send_email",
            {"MessageId": "message-123"},
            {
                "Source": "newsletter@example.com",
                "Destination": {"ToAddresses": ["person@example.com"]},
                "Message": {
                    "Subject": {"Charset": "UTF-8", "Data": "Welcome"},
                    "Body": {
                        "Html": {"Charset": "UTF-8", "Data": "<p>Hello</p>"},
                        "Text": {"Charset": "UTF-8", "Data": "Hello"},
                    },
                },
            },
        )

        message_id = send_email(
            ses_client=client,
            source="newsletter@example.com",
            to_email="person@example.com",
            subject="Welcome",
            html_body="<p>Hello</p>",
            text_body="Hello",
        )

    assert message_id == "message-123"


@override_settings(AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="", SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND=0)
def test_send_email_uses_reply_to_address():
    client = boto3.client(
        "ses",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "send_email",
            {"MessageId": "message-123"},
            {
                "Source": "newsletter@example.com",
                "Destination": {"ToAddresses": ["person@example.com"]},
                "Message": {
                    "Subject": {"Charset": "UTF-8", "Data": "Welcome"},
                    "Body": {
                        "Html": {"Charset": "UTF-8", "Data": "<p>Hello</p>"},
                    },
                },
                "ReplyToAddresses": ["support@example.com"],
            },
        )

        message_id = send_email(
            ses_client=client,
            source="newsletter@example.com",
            to_email="person@example.com",
            subject="Welcome",
            html_body="<p>Hello</p>",
            reply_to="support@example.com",
        )

    assert message_id == "message-123"


@override_settings(AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="", SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND=0)
def test_send_email_uses_cc_and_bcc_addresses():
    client = boto3.client(
        "ses",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "send_email",
            {"MessageId": "message-123"},
            {
                "Source": "newsletter@example.com",
                "Destination": {
                    "ToAddresses": ["person@example.com"],
                    "CcAddresses": ["mentor@example.com"],
                    "BccAddresses": ["audit@example.com"],
                },
                "Message": {
                    "Subject": {"Charset": "UTF-8", "Data": "Welcome"},
                    "Body": {
                        "Html": {"Charset": "UTF-8", "Data": "<p>Hello</p>"},
                    },
                },
            },
        )

        message_id = send_email(
            ses_client=client,
            source="newsletter@example.com",
            to_email="person@example.com",
            subject="Welcome",
            html_body="<p>Hello</p>",
            cc=["mentor@example.com"],
            bcc=["audit@example.com"],
        )

    assert message_id == "message-123"


@override_settings(AWS_SES_CONFIGURATION_SET="", SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND=0)
def test_send_email_uses_raw_message_for_headers_and_structured_parts():
    client = FakeSesClient()

    message_id = send_email(
        ses_client=client,
        source="newsletter@example.com",
        to_email="person@example.com",
        subject="Calendar invite",
        html_body="<p>Join</p>",
        text_body="Join",
        reply_to="support@example.com",
        cc=["mentor@example.com"],
        bcc=["audit@example.com"],
        headers={"X-Calendar-UID": "event-123"},
        message_parts=[
            {
                "content_type": "text/calendar; method=REQUEST",
                "content": "BEGIN:VCALENDAR\nMETHOD:REQUEST\nEND:VCALENDAR",
                "filename": "invite.ics",
                "disposition": "attachment",
            }
        ],
    )

    assert message_id == "raw-message-123"
    assert client.raw_params["Source"] == "newsletter@example.com"
    assert client.raw_params["Destinations"] == [
        "person@example.com",
        "mentor@example.com",
        "audit@example.com",
    ]
    message = message_from_bytes(client.raw_params["RawMessage"]["Data"])
    assert message["From"] == "newsletter@example.com"
    assert message["To"] == "person@example.com"
    assert message["Cc"] == "mentor@example.com"
    assert message["Reply-To"] == "support@example.com"
    assert message["X-Calendar-UID"] == "event-123"
    payload = message.get_payload()
    calendar = payload[-1]
    assert calendar.get_content_type() == "text/calendar"
    assert calendar.get_param("method") == "REQUEST"
    assert calendar.get_filename() == "invite.ics"
    assert "BEGIN:VCALENDAR" in calendar.get_payload()


@pytest.mark.django_db(transaction=True)
@override_settings(
    AWS_REGION="us-east-1",
    AWS_SES_CONFIGURATION_SET="",
    SES_ACCOUNT_MAX_SEND_RATE_PER_SECOND=5.0,
    SES_LIMITER_BURST_MESSAGES=10.0,
)
def test_send_email_acquires_global_permit_and_send_raw_email_does_too():
    """AC2 (behavioral half): both SES send paths consume the token bucket."""
    from mailing.models import SesRateLimitBucket  # noqa: PLC0415
    from mailing.ses_rate_limiter import bucket_key  # noqa: PLC0415

    acquire_warmup = SesRateLimitBucket.objects.filter(key=bucket_key()).first()
    assert acquire_warmup is None  # each test gets a fresh bucket row
    before_tokens = 10.0  # full burst bucket created on first acquire below

    client = boto3.client(
        "ses",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "send_email",
            {"MessageId": "message-1"},
            {
                "Source": "newsletter@example.com",
                "Destination": {"ToAddresses": ["person@example.com"]},
                "Message": {
                    "Subject": {"Charset": "UTF-8", "Data": "Welcome"},
                    "Body": {"Html": {"Charset": "UTF-8", "Data": "<p>Hello</p>"}},
                },
            },
        )
        assert send_email(
            ses_client=client,
            source="newsletter@example.com",
            to_email="person@example.com",
            subject="Welcome",
            html_body="<p>Hello</p>",
        ) == "message-1"

    assert send_raw_email(
        ses_client=_RawStubClient(),
        source="newsletter@example.com",
        to_email="person@example.com",
        subject="Welcome",
        html_body="<p>Hello</p>",
    ) == "raw-message-123"
    after_tokens = SesRateLimitBucket.objects.get(key=bucket_key()).tokens

    # Two sends consumed two tokens; refill at 5/s over a few ms is < 0.5.
    assert after_tokens == pytest.approx(before_tokens - 2, abs=0.5)


class _RawStubClient:
    def send_raw_email(self, **params):
        assert params["Destinations"] == ["person@example.com"]
        return {"MessageId": "raw-message-123"}
