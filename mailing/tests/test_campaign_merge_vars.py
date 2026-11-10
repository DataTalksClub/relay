"""Per-recipient campaign merge vars ({{ name }} from recipient-list member metadata)."""

from email.parser import BytesParser
from email.policy import default

import pytest
from django.test import override_settings
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    Client,
    Contact,
    Organization,
    RecipientList,
    RecipientListMember,
    Subscription,
    SubscriptionStatus,
)
from mailing.services.api_errors import ApiValidationError
from mailing.services.campaign_sender import send_campaign_batch, send_campaign_test_message
from mailing.services.campaigns import CampaignNotReady, campaign_send_issues, queue_campaign

pytestmark = pytest.mark.django_db

SETTINGS = {
    "AWS_REGION": "us-east-1",
    "AWS_SES_CONFIGURATION_SET": "campaign-events",
    "DEFAULT_FROM_EMAIL": "newsletter@example.com",
    "PUBLIC_BASE_URL": "https://mail.example.com",
}
HTML = '<p>Invite people: <a href="{{ invite_url }}">{{invite_url}}</a></p>'
TEXT = "Here is your link for that: {{ invite_url }}"


class _SesStub:
    def __init__(self):
        self.sent = []

    def send_raw_email(self, **params):
        self.sent.append(params)
        return {"MessageId": f"ses-{len(self.sent)}"}

    def send_email(self, **params):
        self.sent.append(params)
        return {"MessageId": f"ses-{len(self.sent)}"}


@pytest.fixture
def setup():
    org = Organization.objects.create(name="DTC", slug="dtc")
    audience = Audience.objects.create(organization=org, name="Courses", slug="courses")
    client = Client.objects.create(organization=org, name="CMP", slug="cmp")
    rl = RecipientList.objects.create(audience=audience, client=client, key="genmedia", name="GenMedia")
    campaign = Campaign.objects.create(
        audience=audience, client=client, subject="Q&A", status=CampaignStatus.DRAFT,
        recipient_list_key=rl.key, html_body=HTML, text_body=TEXT,
    )
    return campaign, rl, audience, client


def _member(rl, audience, client, email, merge_vars):
    contact = Contact.objects.create(email=email, verified_at=timezone.now())
    Subscription.objects.create(contact=contact, audience=audience, client=client, status=SubscriptionStatus.SUBSCRIBED)
    metadata = {} if merge_vars is None else {"merge_vars": merge_vars}
    RecipientListMember.objects.create(
        recipient_list=rl, contact=contact, email=email, source_object_key=email, metadata=metadata,
    )
    return contact


def _bodies(params):
    message = BytesParser(policy=default).parsebytes(params["RawMessage"]["Data"])
    return (
        message.get_body(preferencelist=("html",)).get_content(),
        message.get_body(preferencelist=("plain",)).get_content(),
    )


@pytest.mark.parametrize("mime_template", [True, False])
@override_settings(**SETTINGS)
def test_each_recipient_gets_own_invite_url(setup, settings, mime_template):
    settings.RELAY_MIME_TEMPLATE_ENABLED = mime_template
    campaign, rl, audience, client = setup
    _member(rl, audience, client, "a@example.com", {"invite_url": "https://c.example/register/x/?ref=1&u=a"})
    _member(rl, audience, client, "b@example.com", {"invite_url": "https://c.example/register/x/?ref=2"})
    result = queue_campaign(campaign)
    assert result.recipient_count == 2

    ses = _SesStub()
    ids = list(CampaignRecipient.objects.order_by("email").values_list("id", flat=True))
    sent = send_campaign_batch(
        {"contract": "campaign-email", "version": 1, "campaign_id": campaign.id,
         "batch_id": "b1", "campaign_recipient_ids": ids},
        ses_client=ses,
    )
    assert sent.sent_count == 2
    by_to = {p["Destinations"][0]: _bodies(p) for p in ses.sent}
    html_a, text_a = by_to["a@example.com"]
    html_b, text_b = by_to["b@example.com"]
    assert 'href="https://c.example/register/x/?ref=1&amp;u=a"' in html_a
    assert ">https://c.example/register/x/?ref=1&amp;u=a</a>" in html_a
    assert "your link for that: https://c.example/register/x/?ref=1&u=a" in text_a
    assert "?ref=2" in html_b and "?ref=2" in text_b and "ref=1" not in html_b + text_b
    for body in (html_a, text_a, html_b, text_b):
        assert "{{" not in body and "RLYMRG" not in body


@override_settings(**SETTINGS)
@pytest.mark.parametrize("merge_vars", [None, {}, {"invite_url": ""}, {"invite_url": "  "}, {"other": "x"}])
def test_queue_refuses_when_any_recipient_lacks_a_merge_var(setup, merge_vars):
    campaign, rl, audience, client = setup
    _member(rl, audience, client, "a@example.com", {"invite_url": "https://c.example/?ref=1"})
    _member(rl, audience, client, "b@example.com", merge_vars)

    with pytest.raises(CampaignNotReady, match="missing merge vars"):
        queue_campaign(campaign)

    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.DRAFT
    assert not CampaignRecipient.objects.filter(campaign=campaign).exists()


@override_settings(**SETTINGS)
def test_member_losing_merge_var_after_queue_fails_without_sending(setup):
    campaign, rl, audience, client = setup
    _member(rl, audience, client, "a@example.com", {"invite_url": "https://c.example/?ref=1"})
    queue_campaign(campaign)
    RecipientListMember.objects.update(metadata={})

    ses = _SesStub()
    recipient = CampaignRecipient.objects.get()
    result = send_campaign_batch(
        {"contract": "campaign-email", "version": 1, "campaign_id": campaign.id,
         "batch_id": "b1", "campaign_recipient_ids": [recipient.id]},
        ses_client=ses,
    )
    assert ses.sent == []
    assert result.failed_count == 1
    recipient.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.FAILED
    assert "merge var" in recipient.last_error


@override_settings(**SETTINGS)
def test_static_merge_tag_issues(setup):
    campaign, _, _, _ = setup
    campaign.subject = "Hi {{ name }}"
    assert any("subject" in issue for issue in campaign_send_issues(campaign))
    campaign.subject = "Hi"
    campaign.text_body = "broken {{ invite url }}"
    assert any("malformed" in issue for issue in campaign_send_issues(campaign))
    campaign.text_body = TEXT
    campaign.recipient_list_key = ""
    assert any("recipient list" in issue for issue in campaign_send_issues(campaign))


@override_settings(**SETTINGS)
def test_test_send_uses_member_values_or_fails_loudly(setup):
    campaign, rl, audience, client = setup
    _member(rl, audience, client, "qa@example.com", {"invite_url": "https://c.example/?ref=9"})
    ses = _SesStub()
    send_campaign_test_message(campaign, "qa@example.com", ses_client=ses)
    params = ses.sent[0]
    payload = str(params)
    assert "?ref=9" in payload and "{{" not in payload

    with pytest.raises(ApiValidationError):
        send_campaign_test_message(campaign, "stranger@example.com", ses_client=ses)
    assert len(ses.sent) == 1
