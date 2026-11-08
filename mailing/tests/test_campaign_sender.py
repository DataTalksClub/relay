import json
import re
from email.parser import BytesParser
from email.policy import default

import pytest
from botocore.exceptions import ClientError
from django.test import override_settings

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    Organization,
)
from mailing.services.campaign_sender import (
    build_campaign_html_body,
    build_campaign_text_body,
    send_campaign_batch,
    send_campaign_test_message,
)
from mailing.services.campaigns import campaign_send_issues
from mailing.services.tokens import token_hash
from mailing.sqs import records_from_messages
from mailing.workers import campaign_email_handler

pytestmark = pytest.mark.django_db


@pytest.fixture
def campaign_setup():
    organization = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    audience = Audience.objects.create(organization=organization, name="Newsletter", slug="newsletter")
    client = Client.objects.create(organization=organization, name="Datamailer", slug="datamailer")
    campaign = Campaign.objects.create(
        audience=audience,
        client=client,
        subject="Weekly update",
        status="queued",
        html_body='<p>Hello <a href="https://example.com/post?a=1&b=2">read</a> <a href="mailto:a@example.com">mail</a></p>',
        text_body="Hello in text",
    )
    contact = Contact.objects.create(email="person@example.com")
    recipient = CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=contact.email)
    return campaign, recipient


@override_settings(
    AWS_REGION="us-east-1",
    AWS_SES_CONFIGURATION_SET="campaign-events",
    DEFAULT_FROM_EMAIL="newsletter@example.com",
    PUBLIC_BASE_URL="https://mail.example.com",
)
def test_campaign_sender_sends_pending_recipient_and_persists_audit_state(campaign_setup):
    campaign, recipient = campaign_setup
    ses = _RawSesClient()
    result = send_campaign_batch(_payload(campaign, recipient), ses_client=ses)

    assert ses.params["Source"] == "newsletter@example.com"
    assert ses.params["Destinations"] == ["person@example.com"]
    assert ses.params["ConfigurationSetName"] == "campaign-events"
    raw_message = BytesParser(policy=default).parsebytes(ses.params["RawMessage"]["Data"])
    html_body = raw_message.get_body(preferencelist=("html",)).get_content()
    text_body = raw_message.get_body(preferencelist=("plain",)).get_content()
    assert (
        _html_body_matcher(
            [
                "https://mail.example.com/t/o/",
                "https://mail.example.com/t/c/",
                "u=https%3A%2F%2Fexample.com%2Fpost%3Fa%3D1%26b%3D2",
                'href="mailto:a@example.com"',
                "Unsubscribe or manage preferences",
            ]
        )
        == html_body
    )
    assert (
        _text_body_matcher(
            [
                "Hello in text",
                "Unsubscribe or manage preferences: https://mail.example.com/unsubscribe/",
            ]
        )
        == text_body
    )
    assert raw_message["List-Unsubscribe"].startswith("<https://mail.example.com/unsubscribe/")
    assert raw_message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    recipient.refresh_from_db()
    campaign.refresh_from_db()
    assert result.sent_count == 1
    assert recipient.status == CampaignRecipientStatus.SENT
    assert recipient.ses_message_id == "ses-message-1"
    assert recipient.sent_at is not None
    assert recipient.last_error == ""
    assert campaign.sent_count == 1
    event = EmailEvent.objects.get(campaign_recipient=recipient)
    assert event.event_type == EmailEventType.SENT
    assert event.metadata == {"ses_message_id": "ses-message-1"}


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_campaign_handler_is_idempotent_for_duplicate_sqs_delivery(campaign_setup, monkeypatch):
    campaign, recipient = campaign_setup
    sends = []

    class FakeSes:
        def send_email(self, **params):
            sends.append(params)
            return {"MessageId": "ses-message-1"}

        def send_raw_email(self, **params):
            return self.send_email(**_raw_to_ses_params(params))

    monkeypatch.setattr("mailing.services.campaign_sender.default_ses_client", lambda: FakeSes())

    event = _event_from_payloads([("message-1", _payload(campaign, recipient))])

    assert campaign_email_handler(event, None) == {"batchItemFailures": []}
    assert campaign_email_handler(event, None) == {"batchItemFailures": []}

    recipient.refresh_from_db()
    campaign.refresh_from_db()
    assert len(sends) == 1
    assert recipient.status == CampaignRecipientStatus.SENT
    assert campaign.sent_count == 1
    assert EmailEvent.objects.filter(campaign_recipient=recipient, event_type=EmailEventType.SENT).count() == 1


@pytest.mark.parametrize(
    "status",
    [
        CampaignRecipientStatus.SENT,
        CampaignRecipientStatus.SKIPPED,
        CampaignRecipientStatus.FAILED,
        CampaignRecipientStatus.BOUNCED,
        CampaignRecipientStatus.COMPLAINED,
        CampaignRecipientStatus.UNSUBSCRIBED,
    ],
)
def test_campaign_sender_acknowledges_non_pending_terminal_recipients_without_ses(campaign_setup, status):
    campaign, recipient = campaign_setup
    recipient.status = status
    recipient.ses_message_id = "already-sent" if status == CampaignRecipientStatus.SENT else ""
    recipient.save()

    result = send_campaign_batch(_payload(campaign, recipient), ses_client=_FailingIfCalledSes())

    assert result.skipped_count == 1
    assert EmailEvent.objects.count() == 0


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_campaign_sender_records_permanent_failure_and_acknowledges_record(campaign_setup, monkeypatch):
    campaign, recipient = campaign_setup

    class RejectingSes:
        def send_email(self, **params):
            raise ClientError({"Error": {"Code": "MessageRejected", "Message": "Address rejected"}}, "SendEmail")

        def send_raw_email(self, **params):
            return self.send_email(**_raw_to_ses_params(params))

    monkeypatch.setattr("mailing.services.campaign_sender.default_ses_client", lambda: RejectingSes())
    event = _event_from_payloads([("message-1", _payload(campaign, recipient))])

    assert campaign_email_handler(event, None) == {"batchItemFailures": []}

    recipient.refresh_from_db()
    campaign.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.FAILED
    assert "MessageRejected" in recipient.last_error
    assert campaign.sent_count == 0
    assert EmailEvent.objects.get(campaign_recipient=recipient).event_type == EmailEventType.FAILED


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_campaign_handler_returns_partial_batch_failure_for_retryable_send(campaign_setup, monkeypatch):
    campaign, recipient = campaign_setup
    second_contact = Contact.objects.create(email="second@example.com")
    second_recipient = CampaignRecipient.objects.create(
        campaign=campaign, contact=second_contact, email=second_contact.email
    )

    class MixedSes:
        def send_email(self, **params):
            if params["Destination"]["ToAddresses"] == ["person@example.com"]:
                raise ClientError(
                    {
                        "Error": {"Code": "Throttling", "Message": "Rate exceeded"},
                        "ResponseMetadata": {"HTTPStatusCode": 429},
                    },
                    "SendEmail",
                )
            return {"MessageId": "ses-message-2"}

        def send_raw_email(self, **params):
            return self.send_email(**_raw_to_ses_params(params))

    monkeypatch.setattr("mailing.services.campaign_sender.default_ses_client", lambda: MixedSes())
    event = _event_from_payloads(
        [
            ("retry-message", _payload(campaign, recipient)),
            ("ok-message", _payload(campaign, second_recipient)),
        ]
    )

    assert campaign_email_handler(event, None) == {
        "batchItemFailures": [{"itemIdentifier": "retry-message"}],
    }

    recipient.refresh_from_db()
    second_recipient.refresh_from_db()
    campaign.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.PENDING
    assert "Throttling" in recipient.last_error
    assert second_recipient.status == CampaignRecipientStatus.SENT
    assert campaign.sent_count == 1


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_campaign_handler_refreshes_sent_count_when_single_record_partially_sends_before_retry(
    campaign_setup, monkeypatch
):
    campaign, recipient = campaign_setup
    second_contact = Contact.objects.create(email="second@example.com")
    second_recipient = CampaignRecipient.objects.create(
        campaign=campaign, contact=second_contact, email=second_contact.email
    )

    class MixedSes:
        def send_email(self, **params):
            if params["Destination"]["ToAddresses"] == ["second@example.com"]:
                raise ClientError(
                    {
                        "Error": {"Code": "Throttling", "Message": "Rate exceeded"},
                        "ResponseMetadata": {"HTTPStatusCode": 429},
                    },
                    "SendEmail",
                )
            return {"MessageId": "ses-message-1"}

        def send_raw_email(self, **params):
            return self.send_email(**_raw_to_ses_params(params))

    monkeypatch.setattr("mailing.services.campaign_sender.default_ses_client", lambda: MixedSes())
    payload = _payload(campaign, recipient) | {"campaign_recipient_ids": [recipient.id, second_recipient.id]}
    event = _event_from_payloads([("mixed-message", payload)])

    assert campaign_email_handler(event, None) == {
        "batchItemFailures": [{"itemIdentifier": "mixed-message"}],
    }

    recipient.refresh_from_db()
    second_recipient.refresh_from_db()
    campaign.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.SENT
    assert recipient.ses_message_id == "ses-message-1"
    assert second_recipient.status == CampaignRecipientStatus.PENDING
    assert "Throttling" in second_recipient.last_error
    assert campaign.sent_count == 1


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_campaign_handler_can_retry_pending_recipient_with_token_hashes_after_transient_failure(
    campaign_setup,
    monkeypatch,
):
    campaign, recipient = campaign_setup
    sends = []

    class FlakySes:
        def send_email(self, **params):
            sends.append(params)
            if len(sends) == 1:
                raise ClientError(
                    {
                        "Error": {"Code": "Throttling", "Message": "Rate exceeded"},
                        "ResponseMetadata": {"HTTPStatusCode": 429},
                    },
                    "SendEmail",
                )
            return {"MessageId": "ses-message-2"}

        def send_raw_email(self, **params):
            return self.send_email(**_raw_to_ses_params(params))

    monkeypatch.setattr("mailing.services.campaign_sender.default_ses_client", lambda: FlakySes())
    event = _event_from_payloads([("retry-message", _payload(campaign, recipient))])

    assert campaign_email_handler(event, None) == {
        "batchItemFailures": [{"itemIdentifier": "retry-message"}],
    }

    recipient.refresh_from_db()
    recipient.tracking_token_hash = recipient.tracking_token_hash or token_hash("stale-tracking-token")
    recipient.unsubscribe_token_hash = recipient.unsubscribe_token_hash or token_hash("stale-unsubscribe-token")
    recipient.save(update_fields=["tracking_token_hash", "unsubscribe_token_hash", "updated_at"])

    assert campaign_email_handler(event, None) == {"batchItemFailures": []}

    recipient.refresh_from_db()
    campaign.refresh_from_db()
    assert len(sends) == 2
    assert recipient.status == CampaignRecipientStatus.SENT
    assert recipient.ses_message_id == "ses-message-2"
    assert campaign.sent_count == 1
    assert EmailEvent.objects.filter(campaign_recipient=recipient, event_type=EmailEventType.SENT).count() == 1

    html_body = sends[1]["Message"]["Body"]["Html"]["Data"]
    text_body = sends[1]["Message"]["Body"]["Text"]["Data"]
    tracking_token = re.search(r"/t/o/([A-Za-z0-9_-]+)\.gif", html_body).group(1)
    unsubscribe_token = re.search(r"/unsubscribe/([A-Za-z0-9_-]+)", html_body).group(1)
    assert token_hash(tracking_token) == recipient.tracking_token_hash
    assert token_hash(unsubscribe_token) == recipient.unsubscribe_token_hash
    assert f"https://mail.example.com/t/c/{tracking_token}?u=" in html_body
    assert f"https://mail.example.com/unsubscribe/{unsubscribe_token}" in text_body


def test_campaign_sender_rejects_recipient_ids_from_another_campaign(campaign_setup):
    campaign, recipient = campaign_setup
    other_campaign = Campaign.objects.create(
        audience=campaign.audience,
        client=campaign.client,
        subject="Other",
    )
    other_recipient = CampaignRecipient.objects.create(
        campaign=other_campaign, contact=recipient.contact, email=recipient.email
    )

    event = _event_from_payloads([("bad-message", _payload(campaign, other_recipient))])

    assert campaign_email_handler(event, None) == {
        "batchItemFailures": [{"itemIdentifier": "bad-message"}],
    }


@override_settings(DEBUG=False, TESTING=False, PUBLIC_BASE_URL="http://relay.example.com")
def test_live_campaign_requires_https_public_base_url(campaign_setup):
    campaign, _recipient = campaign_setup

    assert "Set PUBLIC_BASE_URL to HTTPS before sending campaigns." in campaign_send_issues(campaign)


def test_campaign_preflight_rejects_unresolved_mailchimp_merge_tags(campaign_setup):
    campaign, _recipient = campaign_setup
    campaign.html_body = "<html><body>*|HTML:LIST_ADDRESS|* *|UNSUB|*</body></html>"

    assert "Resolve Mailchimp merge tags before sending through Relay." in campaign_send_issues(campaign)


@override_settings(PUBLIC_BASE_URL="https://mail.example.com")
def test_build_campaign_html_body_adds_tracking_pixel_click_links_and_unsubscribe_link():
    html = build_campaign_html_body(
        '<a href="https://example.com/path">tracked</a><a href="/relative">relative</a>',
        "tracking-token",
        "unsubscribe-token",
    )

    assert 'src="https://mail.example.com/t/o/tracking-token.gif"' in html
    assert 'href="https://mail.example.com/t/c/tracking-token?u=https%3A%2F%2Fexample.com%2Fpath"' in html
    assert 'href="/relative"' in html
    assert 'href="https://mail.example.com/unsubscribe/unsubscribe-token"' in html


def _payload(campaign, recipient):
    return {
        "contract": "campaign-email",
        "version": 1,
        "campaign_id": campaign.id,
        "batch_id": f"campaign-{campaign.id}-batch-1",
        "campaign_recipient_ids": [recipient.id],
    }


def _event_from_payloads(message_payloads):
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


class _RawSesClient:
    def __init__(self):
        self.params = None

    def send_raw_email(self, **params):
        self.params = params
        return {"MessageId": "ses-message-1"}


def _raw_to_ses_params(params):
    message = BytesParser(policy=default).parsebytes(params["RawMessage"]["Data"])
    body = {}
    for part in message.walk():
        if part.get_content_type() == "text/plain":
            body["Text"] = {"Charset": "UTF-8", "Data": part.get_content()}
        elif part.get_content_type() == "text/html":
            body["Html"] = {"Charset": "UTF-8", "Data": part.get_content()}
    result = {
        "Source": params["Source"],
        "Destination": {"ToAddresses": [message["To"]]},
        "Message": {
            "Subject": {"Charset": "UTF-8", "Data": str(message["Subject"])},
            "Body": body,
        },
    }
    if "ConfigurationSetName" in params:
        result["ConfigurationSetName"] = params["ConfigurationSetName"]
    return result


class _FailingIfCalledSes:
    def send_email(self, **params):
        raise AssertionError("SES should not be called")


class _html_body_matcher:
    def __init__(self, expected_parts):
        self.expected_parts = expected_parts

    def __eq__(self, other):
        return all(part in other for part in self.expected_parts)


class _text_body_matcher:
    def __init__(self, expected_parts):
        self.expected_parts = expected_parts

    def __eq__(self, other):
        return all(part in other for part in self.expected_parts)


def test_full_document_tracking_and_unsubscribe_stay_inside_body():
    document = "<!doctype html><html><head><title>Email</title></head><BODY><p>Hello</p></BODY></html>"
    rendered = build_campaign_html_body(document, "tracking", "unsubscribe")
    lowered = rendered.lower()
    body_end = lowered.rindex("</body>")
    assert rendered.index("Unsubscribe or manage preferences") < body_end
    assert rendered.index('width="1" height="1"') < body_end
    assert lowered.endswith("</body></html>")
    assert lowered.count("</body>") == 1


def test_campaign_bodies_replace_mailchimp_uniqid_footer_links():
    html = (
        '<html><body><a href="https://mailchi.mp/datatalks.club/weekly-301?e=[UNIQID]">View</a>'
        '<a href="https://us19.forward-to-friend.com/forward?u=1&id=2&e=[UNIQID]">Forward to a friend</a> · '
        '<a href="https://club.us19.list-manage.com/profile?u=1&id=2&e=[UNIQID]&c=3">update your preferences</a>'
        '<a href="https://club.us19.list-manage.com/unsubscribe?u=1&id=2&t=b&e=[UNIQID]&c=3">unsubscribe</a>'
        "</body></html>"
    )
    text = (
        "View (https://mailchi.mp/datatalks.club/weekly-301?e=[UNIQID])\n"
        "Forward to a friend (https://us19.forward-to-friend.com/forward?u=1&id=2&e=[UNIQID])\n"
        "unsubscribe (https://club.us19.list-manage.com/unsubscribe?u=1&id=2&t=b&e=[UNIQID]&c=3)\n"
    )
    out_html = build_campaign_html_body(html, "track-tok", "unsub-tok")
    out_text = build_campaign_text_body(text, "unsub-tok")

    assert "UNIQID" not in out_html
    assert "forward-to-friend" not in out_html
    assert "list-manage" not in out_html
    assert out_html.count("/unsubscribe/unsub-tok") == 3  # preferences, unsubscribe, appended footer
    assert "UNIQID" not in out_text
    assert "forward-to-friend" not in out_text
    assert "list-manage" not in out_text
    assert "/unsubscribe/unsub-tok" in out_text


class _CapturingSes:
    def __init__(self):
        self.params = None

    def send_email(self, **params):
        self.params = params
        return {"MessageId": "test-message-1"}


@override_settings(DEFAULT_FROM_EMAIL="relay@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_campaign_test_send_uses_client_default_sender(campaign_setup):
    campaign, _recipient = campaign_setup
    client = campaign.client
    client.sender_emails = [{"id": "alexey", "email": "Alexey Grigorev <alexey@datatalks.club>"}]
    client.default_sender_id = "alexey"
    client.save(update_fields=["sender_emails", "default_sender_id", "updated_at"])
    ses = _CapturingSes()

    assert send_campaign_test_message(campaign, "test@example.com", ses_client=ses) == "test-message-1"
    assert ses.params["Source"] == "Alexey Grigorev <alexey@datatalks.club>"
    assert ses.params["Destination"] == {"ToAddresses": ["test@example.com"]}


@override_settings(DEFAULT_FROM_EMAIL="relay@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_campaign_test_send_falls_back_to_default_from_without_sender(campaign_setup):
    campaign, _recipient = campaign_setup
    ses = _CapturingSes()

    send_campaign_test_message(campaign, "test@example.com", ses_client=ses)

    assert ses.params["Source"] == "relay@example.com"


@override_settings(DEFAULT_FROM_EMAIL="relay@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_campaign_test_send_explicit_source_wins_over_client_sender(campaign_setup):
    campaign, _recipient = campaign_setup
    client = campaign.client
    client.sender_emails = [{"id": "alexey", "email": "Alexey Grigorev <alexey@datatalks.club>"}]
    client.default_sender_id = "alexey"
    client.save(update_fields=["sender_emails", "default_sender_id", "updated_at"])
    ses = _CapturingSes()

    send_campaign_test_message(campaign, "test@example.com", ses_client=ses, source="explicit@example.com")

    assert ses.params["Source"] == "explicit@example.com"
