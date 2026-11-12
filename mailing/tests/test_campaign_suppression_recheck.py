"""Suppression recheck: state changes after the snapshot must block the send.

A recipient PENDING at snapshot time may unsubscribe, hard-bounce or complain
before its batch runs. The sender re-checks the contact and the campaign's
subscription right before the SES call using the snapshot's shared rules, and
marks the recipient SKIPPED without calling SES.
"""

import pytest
from django.test import override_settings
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientSkipReason,
    CampaignRecipientStatus,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    Organization,
    Subscription,
    SubscriptionStatus,
)
from mailing.services.campaign_sender import send_campaign_batch
from mailing.services.campaigns import _skip_reason
from mailing.services.suppression import suppression_skip_reason

pytestmark = pytest.mark.django_db


def _sendable_setup():
    organization = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    audience = Audience.objects.create(organization=organization, name="Newsletter", slug="newsletter")
    client = Client.objects.create(organization=organization, name="Datamailer", slug="datamailer")
    campaign = Campaign.objects.create(
        audience=audience,
        client=client,
        subject="Weekly update",
        status="queued",
        html_body="<p>Hello</p>",
        text_body="Hello in text",
    )
    contact = Contact.objects.create(email="person@example.com", verified_at=timezone.now())
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=client,
        status=SubscriptionStatus.SUBSCRIBED,
    )
    recipient = CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=contact.email)
    return campaign, recipient


def _payload(campaign, recipient):
    return {
        "contract": "campaign-email",
        "version": 1,
        "campaign_id": campaign.id,
        "batch_id": f"campaign-{campaign.id}-batch-1",
        "campaign_recipient_ids": [recipient.id],
    }


class _FailingIfCalledSes:
    def __init__(self):
        self.calls = 0

    def send_email(self, **params):
        self.calls += 1
        raise AssertionError("SES must not be called for a suppressed recipient")

    def send_raw_email(self, **params):
        self.calls += 1
        raise AssertionError("SES must not be called for a suppressed recipient")


class _CapturingSes:
    def __init__(self):
        self.calls = 0

    def send_email(self, **params):
        self.calls += 1
        return {"MessageId": "ses-message-1"}

    def send_raw_email(self, **params):
        self.calls += 1
        return {"MessageId": "ses-message-1"}


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
@pytest.mark.parametrize("scope", ["client", "audience"])
def test_contact_unsubscribes_after_snapshot_is_not_sent(scope):
    campaign, recipient = _sendable_setup()
    if scope == "audience":
        Subscription.objects.create(
            contact=recipient.contact,
            audience=campaign.audience,
            client=None,
            status=SubscriptionStatus.UNSUBSCRIBED,
        )
    else:
        Subscription.objects.filter(
            contact=recipient.contact, audience=campaign.audience, client=campaign.client
        ).update(status=SubscriptionStatus.UNSUBSCRIBED)
    ses = _FailingIfCalledSes()

    result = send_campaign_batch(_payload(campaign, recipient), ses_client=ses)

    expected = (
        CampaignRecipientSkipReason.AUDIENCE_UNSUBSCRIBE
        if scope == "audience"
        else CampaignRecipientSkipReason.CLIENT_UNSUBSCRIBE
    )
    assert ses.calls == 0
    assert (result.sent_count, result.skipped_count, result.failed_count) == (0, 1, 0)
    recipient.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.SKIPPED
    assert recipient.skip_reason == expected
    assert recipient.last_error.startswith("suppressed_after_snapshot:")
    assert recipient.ses_message_id == ""
    campaign.refresh_from_db()
    assert campaign.sent_count == 0
    event = EmailEvent.objects.get(campaign_recipient=recipient)
    assert event.event_type == EmailEventType.SKIPPED
    assert event.metadata["suppressed_after_snapshot"] is True


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("hard_bounced_at", CampaignRecipientSkipReason.HARD_BOUNCE),
        ("complained_at", CampaignRecipientSkipReason.COMPLAINT),
    ],
)
def test_bounced_or_complained_after_snapshot_is_not_sent(field, expected):
    campaign, recipient = _sendable_setup()
    Contact.objects.filter(pk=recipient.contact_id).update(**{field: timezone.now()})
    ses = _FailingIfCalledSes()

    result = send_campaign_batch(_payload(campaign, recipient), ses_client=ses)

    assert ses.calls == 0
    assert (result.sent_count, result.skipped_count, result.failed_count) == (0, 1, 0)
    recipient.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.SKIPPED
    assert recipient.skip_reason == expected
    assert recipient.last_error.startswith("suppressed_after_snapshot:")
    campaign.refresh_from_db()
    assert campaign.sent_count == 0
    assert EmailEvent.objects.get(campaign_recipient=recipient).event_type == EmailEventType.SKIPPED


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_still_subscribed_contact_is_sent_with_shared_snapshot_rules():
    campaign, recipient = _sendable_setup()
    contact = recipient.contact
    audience_subscription = Subscription.objects.filter(
        contact=contact, audience=campaign.audience, client__isnull=True
    ).first()
    client_subscription = Subscription.objects.get(contact=contact, audience=campaign.audience, client=campaign.client)
    # The sender recheck and the snapshot consult the same shared function.
    assert suppression_skip_reason(contact, audience_subscription, client_subscription) == ""
    assert _skip_reason(contact, campaign) == ""
    ses = _CapturingSes()

    result = send_campaign_batch(_payload(campaign, recipient), ses_client=ses)

    assert ses.calls == 1
    assert (result.sent_count, result.skipped_count, result.failed_count) == (1, 0, 0)
    recipient.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.SENT
    assert recipient.ses_message_id == "ses-message-1"
    campaign.refresh_from_db()
    assert campaign.sent_count == 1
