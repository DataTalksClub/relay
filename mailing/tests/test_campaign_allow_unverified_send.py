"""allow_unverified must survive the pre-SES recheck for explicit-list campaigns.

Regression: an explicit recipient-list campaign with ``allow_unverified=True``
snapshotted its unverified members as PENDING, but the sender's recheck ignored
the flag and skipped every one as ``suppressed_after_snapshot:unverified`` --
zero sent. These tests drive queue_campaign -> campaign queue -> worker.
"""

from unittest.mock import patch

import pytest
from django.test import override_settings
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientSkipReason,
    CampaignRecipientStatus,
    CampaignStatus,
    Client,
    Contact,
    EmailValidationStatus,
    Organization,
    RecipientList,
    RecipientListMember,
    Subscription,
    SubscriptionStatus,
)
from mailing.services.campaigns import queue_campaign
from mailing.tests.task_helpers import drain_queue

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("_send_settings"),
]


@pytest.fixture
def _send_settings():
    with override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com"):
        yield


class _Ses:
    def __init__(self):
        self.to = []

    def send_email(self, **params):
        self.to.extend(params["Destination"]["ToAddresses"])
        return {"MessageId": f"ses-{len(self.to)}"}

    def send_raw_email(self, **params):
        self.to.extend(params.get("Destinations") or [params.get("Destination")])
        return {"MessageId": f"ses-{len(self.to)}"}


@pytest.fixture
def scope():
    org = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    audience = Audience.objects.create(organization=org, name="Maven", slug="maven")
    client = Client.objects.create(organization=org, name="Maven", slug="maven")
    recipient_list = RecipientList.objects.create(
        audience=audience, client=client, key="product-shipping", name="Product shipping"
    )
    return audience, client, recipient_list


def _member(scope, email, **contact_fields):
    audience, client, recipient_list = scope
    contact = Contact.objects.create(email=email, **contact_fields)
    Subscription.objects.create(contact=contact, audience=audience, client=client, status=SubscriptionStatus.SUBSCRIBED)
    RecipientListMember.objects.create(
        recipient_list=recipient_list, contact=contact, email=email, source_object_key=email
    )
    return contact


def _campaign(scope, *, recipient_list_key):
    audience, client, _ = scope
    return Campaign.objects.create(
        audience=audience, client=client, external_key="product-shipping", subject="Shipping",
        html_body="<p>Shipping</p>", text_body="Shipping", status=CampaignStatus.DRAFT,
        recipient_list_key=recipient_list_key, allow_unverified=True,
    )


def _queue_and_work(campaign, django_capture_on_commit_callbacks, after_snapshot=None):
    with django_capture_on_commit_callbacks(execute=True):
        result = queue_campaign(campaign)
    assert result.queued is True
    if after_snapshot:
        after_snapshot()
    ses = _Ses()
    with patch("mailing.services.campaign_sender.default_ses_client", return_value=ses):
        assert drain_queue("campaign") >= 1
    return ses


def test_explicit_list_allow_unverified_sends_unverified_members(scope, django_capture_on_commit_callbacks):
    emails = ["a@example.com", "b@example.com", "c@example.com"]
    for email in emails:
        _member(scope, email)  # unverified: no verified_at anywhere
    campaign = _campaign(scope, recipient_list_key="product-shipping")

    ses = _queue_and_work(campaign, django_capture_on_commit_callbacks)

    assert sorted(ses.to) == emails
    statuses = set(CampaignRecipient.objects.filter(campaign=campaign).values_list("status", "last_error"))
    assert statuses == {(CampaignRecipientStatus.SENT, "")}
    campaign.refresh_from_db()
    assert campaign.sent_count == 3


def test_explicit_list_recheck_keeps_every_other_guard(scope, django_capture_on_commit_callbacks):
    audience, client, _ = scope
    ok = _member(scope, "ok@example.com")
    bounced = _member(scope, "bounced@example.com")
    complained = _member(scope, "complained@example.com")
    global_unsub = _member(scope, "global@example.com")
    invalid = _member(scope, "invalid@example.com")
    client_unsub = _member(scope, "client@example.com")
    audience_unsub = _member(scope, "audience@example.com")
    pending = _member(scope, "pending@example.com")
    campaign = _campaign(scope, recipient_list_key="product-shipping")

    def change_state_after_snapshot():
        now = timezone.now()
        Contact.objects.filter(pk=bounced.pk).update(hard_bounced_at=now)
        Contact.objects.filter(pk=complained.pk).update(complained_at=now)
        Contact.objects.filter(pk=global_unsub.pk).update(global_unsubscribed_at=now)
        Contact.objects.filter(pk=invalid.pk).update(email_validation_status=EmailValidationStatus.NO_MX)
        Subscription.objects.filter(contact=client_unsub, client=client).update(status=SubscriptionStatus.UNSUBSCRIBED)
        Subscription.objects.create(
            contact=audience_unsub, audience=audience, client=None, status=SubscriptionStatus.UNSUBSCRIBED
        )
        Subscription.objects.filter(contact=pending, client=client).update(status=SubscriptionStatus.PENDING)

    assert CampaignRecipient.objects.count() == 0
    ses = _queue_and_work(campaign, django_capture_on_commit_callbacks, change_state_after_snapshot)

    assert ses.to == [ok.email]
    reasons = dict(
        CampaignRecipient.objects.filter(campaign=campaign, status=CampaignRecipientStatus.SKIPPED)
        .values_list("email", "skip_reason")
    )
    assert reasons == {
        bounced.email: CampaignRecipientSkipReason.HARD_BOUNCE,
        complained.email: CampaignRecipientSkipReason.COMPLAINT,
        global_unsub.email: CampaignRecipientSkipReason.GLOBAL_UNSUBSCRIBE,
        invalid.email: CampaignRecipientSkipReason.INVALID_EMAIL,
        client_unsub.email: CampaignRecipientSkipReason.CLIENT_UNSUBSCRIBE,
        audience_unsub.email: CampaignRecipientSkipReason.AUDIENCE_UNSUBSCRIBE,
        pending.email: CampaignRecipientSkipReason.SUPPRESSED,
    }


def test_allow_unverified_without_explicit_list_still_rechecks_verification(scope, django_capture_on_commit_callbacks):
    """The flag is only honored at send time together with an explicit recipient list."""
    contact = _member(scope, "unverified@example.com")
    campaign = _campaign(scope, recipient_list_key="product-shipping")
    with django_capture_on_commit_callbacks(execute=True):
        queue_campaign(campaign)
    # Not reachable via the API (allow_unverified requires recipient_list_key); simulate drift.
    Campaign.objects.filter(pk=campaign.pk).update(recipient_list_key="")
    ses = _Ses()
    with patch("mailing.services.campaign_sender.default_ses_client", return_value=ses):
        drain_queue("campaign")

    assert ses.to == []
    recipient = CampaignRecipient.objects.get(campaign=campaign, contact=contact)
    assert recipient.status == CampaignRecipientStatus.SKIPPED
    assert recipient.last_error == "suppressed_after_snapshot:unverified"
