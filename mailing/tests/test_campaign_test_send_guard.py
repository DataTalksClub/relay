"""Tests for campaign test-send recipient allowlist, empty-body refusal, and non-member merge vars."""

import json

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
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
from mailing.services.auth import create_client_api_key
from mailing.services.campaign_sender import send_campaign_test_message
from mailing.services.campaigns import campaign_send_issues
from mailing.services.test_send_guard import has_meaningful_body, is_allowed_test_recipient

pytestmark = pytest.mark.django_db

API_KEY = "test-send-guard-api-key"
SETTINGS = {
    "AWS_REGION": "us-east-1",
    "AWS_SES_CONFIGURATION_SET": "campaign-events",
    "DEFAULT_FROM_EMAIL": "newsletter@example.com",
    "PUBLIC_BASE_URL": "https://mail.example.com",
}


class _SesStub:
    def __init__(self):
        self.sent = []

    def send_email(self, **params):
        self.sent.append(params)
        return {"MessageId": f"ses-{len(self.sent)}"}


@pytest.fixture
def env():
    org = Organization.objects.create(name="DTC", slug="dtc")
    audience = Audience.objects.create(organization=org, name="Courses", slug="courses")
    client = Client.objects.create(organization=org, name="CMP", slug="cmp")
    create_client_api_key(client=client, name="cmp-test", raw_api_key=API_KEY)
    rl = RecipientList.objects.create(audience=audience, client=client, key="cohort", name="Cohort")
    campaign = Campaign.objects.create(
        audience=audience,
        client=client,
        external_key="camp-guard",
        subject="Course update",
        status=CampaignStatus.DRAFT,
        recipient_list_key=rl.key,
        html_body="<p>Hello {{ first_name }}, <a href=\"{{ invite_url }}\">join here</a></p>",
        text_body="Hello {{ first_name }}, join here: {{ invite_url }}",
    )
    return campaign, rl, audience, client


def _post_test_send(django_client, external_key, payload):
    return django_client.post(
        reverse("mailing:api_campaign_test_send", args=[external_key]),
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {API_KEY}",
    )


@pytest.mark.parametrize(
    "email",
    [
        "dev+test@dtcdev.click",
        "Dev+CMP-321@DTCDev.Click",
        "success@simulator.amazonses.com",
        "bounce@simulator.amazonses.com",
        "complaint+tag@simulator.amazonses.com",
    ],
)
def test_allowed_test_send_recipients(email):
    assert is_allowed_test_recipient(email) is True


@pytest.mark.parametrize(
    "email",
    [
        "alexey@datatalks.club",
        "alexey.s.grigoriev@gmail.com",
        "dev@dtcdev.click",
        "other@dtcdev.click",
        "user@example.com",
        "dev+@dtcdev.click",
        "",
    ],
)
def test_disallowed_test_send_recipients(email):
    assert is_allowed_test_recipient(email) is False


@override_settings(**SETTINGS)
@pytest.mark.parametrize(
    "blocked_email",
    [
        "alexey@datatalks.club",
        "alexey.s.grigoriev@gmail.com",
        "dev@dtcdev.click",
        "someone@example.com",
    ],
)
def test_api_test_send_refuses_real_addresses(client, env, monkeypatch, blocked_email):
    campaign, _, audience, api_client = env
    calls = []
    monkeypatch.setattr("mailing.services.api.send_campaign_test_message", lambda *a, **kw: calls.append(a))

    response = _post_test_send(
        client,
        campaign.external_key,
        {"audience": audience.slug, "client": api_client.slug, "emails": [blocked_email]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"]["emails"][0]["error"] == "not_allowed_test_recipient"
    assert calls == []


@override_settings(**SETTINGS)
def test_direct_send_campaign_test_message_refuses_real_address(env):
    campaign, _, _, _ = env
    ses = _SesStub()

    with pytest.raises(ApiValidationError) as exc_info:
        send_campaign_test_message(campaign, "alexey@datatalks.club", ses_client=ses)

    assert "dev+...@dtcdev.click" in exc_info.value.errors["email"]
    assert ses.sent == []


@pytest.mark.parametrize(
    ("html_body", "text_body"),
    [
        ("", ""),
        ("   \n\t  ", "   "),
        ("<html><body></body></html>", ""),
        ("<html><body><p>&nbsp;</p><div><br></div></body></html>", ""),
        ("<style>p { color: red; }</style><!-- draft only -->", ""),
    ],
)
def test_has_meaningful_body_rejects_empty_and_shell_drafts(html_body, text_body):
    assert has_meaningful_body(html_body, text_body) is False


@override_settings(**SETTINGS)
@pytest.mark.parametrize(
    ("html_body", "text_body"),
    [
        ("", ""),
        ("<html><body><p>&nbsp;</p></body></html>", ""),
        ("<style>body{margin:0}</style><!-- footer only -->", "   "),
    ],
)
def test_api_and_direct_test_send_refuse_empty_or_footer_only_body(client, env, html_body, text_body):
    campaign, _, audience, api_client = env
    campaign.html_body = html_body
    campaign.text_body = text_body
    campaign.save(update_fields=["html_body", "text_body", "updated_at"])

    assert campaign_send_issues(campaign)

    response = _post_test_send(
        client,
        campaign.external_key,
        {
            "audience": audience.slug,
            "client": api_client.slug,
            "emails": ["dev+empty@dtcdev.click"],
        },
    )
    assert response.status_code == 400
    assert "body" in response.json()["error"]["fields"]

    ses = _SesStub()
    with pytest.raises(ApiValidationError):
        send_campaign_test_message(campaign, "dev+empty@dtcdev.click", ses_client=ses)
    assert ses.sent == []


@override_settings(**SETTINGS)
def test_test_send_with_merge_vars_does_not_require_active_list_member(env):
    campaign, rl, audience, api_client = env
    # Real member on list — test-send must NOT require using this address.
    real_contact = Contact.objects.create(email="alexey@datatalks.club", verified_at=timezone.now())
    Subscription.objects.create(
        contact=real_contact, audience=audience, client=api_client, status=SubscriptionStatus.SUBSCRIBED
    )
    RecipientListMember.objects.create(
        recipient_list=rl,
        contact=real_contact,
        email="alexey@datatalks.club",
        source_object_key="alexey",
        metadata={"merge_vars": {"first_name": "Alexey", "invite_url": "https://real.example/secret-token"}},
    )

    ses = _SesStub()
    send_campaign_test_message(campaign, "dev+nonmember@dtcdev.click", ses_client=ses)

    assert len(ses.sent) == 1
    sent = ses.sent[0]
    assert sent["Destination"] == {"ToAddresses": ["dev+nonmember@dtcdev.click"]}
    html = sent["Message"]["Body"]["Html"]["Data"]
    text = sent["Message"]["Body"]["Text"]["Data"]
    assert "[test:first_name]" in html and "[test:first_name]" in text
    assert "https://example.invalid/test-merge/invite_url" in text
    assert "secret-token" not in html and "secret-token" not in text
    assert "{{" not in html and "{{" not in text
    assert "RLYMRG" not in html and "RLYMRG" not in text


@override_settings(**SETTINGS)
def test_api_test_send_accepts_explicit_merge_vars(client, env, monkeypatch):
    campaign, _, audience, api_client = env
    ses = _SesStub()
    monkeypatch.setattr("mailing.services.campaign_sender.default_ses_client", lambda: ses)

    response = _post_test_send(
        client,
        campaign.external_key,
        {
            "audience": audience.slug,
            "client": api_client.slug,
            "emails": ["success@simulator.amazonses.com"],
            "merge_vars": {"first_name": "QA", "invite_url": "https://c.example/qa"},
        },
    )

    assert response.status_code == 200
    assert response.json()["sent_count"] == 1
    text = ses.sent[0]["Message"]["Body"]["Text"]["Data"]
    assert "Hello QA, join here: https://c.example/qa" in text
