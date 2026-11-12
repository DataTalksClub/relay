import pytest
from django.conf import settings
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from mailing.models import (
    Audience,
    CategoryPreference,
    Client,
    Contact,
    ContactSourceMetadata,
    EmailTemplate,
    Organization,
    Subscription,
    SubscriptionStatus,
    TransactionalMessage,
    TransactionalRequestMinute,
)
from mailing.services.public_lists import parse_public_lists

pytestmark = pytest.mark.django_db(transaction=True)

LISTS = (
    "pocketshell org=pocketshell client=pocketshell audience=pocketshell "
    "category=newsletter template=confirm-signup confirm_base=https://pocketshell.io/"
)
ORIGINS = ["https://pocketshell.io", "http://localhost:4000"]


@pytest.fixture
def pocketshell():
    organization = Organization.objects.create(name="PocketShell", slug="pocketshell")
    client = Client.objects.create(
        organization=organization,
        name="PocketShell",
        slug="pocketshell",
        default_sender_id="hello",
        sender_emails=[{"id": "hello", "email": "PocketShell <hello@pocketshell.io>"}],
    )
    audience = Audience.objects.create(organization=organization, name="PocketShell", slug="pocketshell")
    EmailTemplate.objects.create(
        client=client,
        key="confirm-signup",
        name="Confirm signup",
        subject="Confirm your PocketShell email",
        html_body="<p><a href='{{ confirm_url }}'>Verify</a></p>",
        text_body="Verify: {{ confirm_url }}",
        is_transactional=True,
        is_active=True,
    )
    return {"organization": organization, "client": client, "audience": audience}


def post_public(django_client, action, payload, origin="https://pocketshell.io"):
    return django_client.post(
        reverse(f"mailing:api_public_{action}", kwargs={"list_key": "pocketshell"}),
        data=payload,
        content_type="application/json",
        HTTP_ORIGIN=origin,
    )


@override_settings(RELAY_PUBLIC_LISTS=LISTS, RELAY_PUBLIC_SUBSCRIBE_ORIGINS=frozenset(ORIGINS))
def test_public_subscribe_sends_confirmation_and_confirm_subscribes(client, pocketshell, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    requested = post_public(client, "subscribe", {"email": "Ada@Example.com"})

    assert requested.status_code == 200
    assert requested.json() == {"status": "verification_requested"}
    assert requested["Access-Control-Allow-Origin"] == "https://pocketshell.io"
    assert len(enqueued) == 1
    message = TransactionalMessage.objects.get()
    assert message.from_email == "PocketShell <hello@pocketshell.io>"
    assert message.context["confirm_url"].startswith("https://pocketshell.io?token=")
    assert "Ada@Example.com" not in message.context["confirm_url"]
    subscription = Subscription.objects.get()
    assert subscription.status == SubscriptionStatus.PENDING
    assert subscription.verified_at is None

    confirmed = post_public(client, "confirm", {"token": message.context["verification_token"]})
    again = post_public(client, "confirm", {"token": message.context["verification_token"]})

    assert confirmed.status_code == 200
    assert confirmed.json() == {"status": "subscribed"}
    assert again.status_code == 200
    subscription.refresh_from_db()
    assert subscription.status == SubscriptionStatus.SUBSCRIBED
    assert subscription.verified_at is not None
    contact = Contact.objects.get()
    assert contact.verified_at is not None
    preference = CategoryPreference.objects.get()
    assert preference.tag == "newsletter"
    assert preference.enabled is True


@override_settings(RELAY_PUBLIC_LISTS=LISTS, RELAY_PUBLIC_SUBSCRIBE_ORIGINS=frozenset(ORIGINS))
def test_public_subscribe_of_existing_subscriber_does_not_send(client, pocketshell, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    contact = Contact.objects.create(email="ada@example.com", verified_at=timezone.now())
    Subscription.objects.create(
        contact=contact,
        audience=pocketshell["audience"],
        client=pocketshell["client"],
        status=SubscriptionStatus.SUBSCRIBED,
        verified_at=timezone.now(),
    )

    response = post_public(client, "subscribe", {"email": "ada@example.com"})

    assert response.status_code == 200
    assert response.json() == {"status": "already_subscribed"}
    assert enqueued == []
    assert TransactionalMessage.objects.count() == 0


@override_settings(
    RELAY_PUBLIC_LISTS=LISTS,
    RELAY_PUBLIC_SUBSCRIBE_ORIGINS=frozenset(ORIGINS),
    RELAY_PUBLIC_SUBSCRIBE_EMAIL_LIMIT=1,
)
def test_public_subscribe_rate_limits_repeat_sends(client, pocketshell, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    first = post_public(client, "subscribe", {"email": "ada@example.com"})
    second = post_public(client, "subscribe", {"email": "ada@example.com"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["fields"] == {"email": "rate_limited"}
    assert len(enqueued) == 1


@override_settings(RELAY_PUBLIC_LISTS=LISTS, RELAY_PUBLIC_SUBSCRIBE_ORIGINS=frozenset(ORIGINS))
def test_public_subscribe_rejects_unknown_list_and_bad_email(client, pocketshell):
    missing = client.post(
        reverse("mailing:api_public_subscribe", kwargs={"list_key": "other"}),
        data={"email": "ada@example.com"},
        content_type="application/json",
    )
    invalid = post_public(client, "subscribe", {"email": "not-an-email"})
    anonymous_origin = post_public(client, "subscribe", {"email": "ada@example.com"}, origin="https://evil.example")

    assert missing.status_code == 404
    assert invalid.status_code == 400
    assert invalid.json()["error"]["fields"] == {"email": "invalid"}
    assert "Access-Control-Allow-Origin" not in anonymous_origin


@override_settings(RELAY_PUBLIC_LISTS=LISTS, RELAY_PUBLIC_SUBSCRIBE_ORIGINS=frozenset(ORIGINS))
def test_public_preflight_is_an_empty_204(client):
    response = client.options(
        reverse("mailing:api_public_subscribe", kwargs={"list_key": "pocketshell"}),
        HTTP_ORIGIN="https://pocketshell.io",
    )

    assert response.status_code == 204
    assert response.content == b""
    assert "Content-Length" not in response
    assert response["Access-Control-Allow-Origin"] == "https://pocketshell.io"
    assert response["Access-Control-Allow-Methods"] == "POST, OPTIONS"


def test_parse_public_lists_rejects_a_broken_entry():
    with pytest.raises(Exception):
        parse_public_lists("pocketshell client=only")


def test_parse_public_lists_keeps_agent_git_lab_beside_pocketshell():
    lists = parse_public_lists(
        "pocketshell org=pocketshell client=pocketshell audience=pocketshell "
        "category=newsletter template=confirm-signup confirm_base=https://pocketshell.io/; "
        "agent-git-lab org=agent-git-lab client=agent-git-lab audience=agent-git-lab "
        "category=newsletter template=confirm-signup "
        "confirm_base=https://alexeygrigorev.com/cloudflare-agent-git/subscribe/"
    )

    assert lists["pocketshell"].confirm_base_url == "https://pocketshell.io"
    lab = lists["agent-git-lab"]
    assert lab.client_slug == "agent-git-lab"
    assert lab.audience_slug == "agent-git-lab"
    assert lab.organization_slug == "agent-git-lab"
    assert lab.confirm_base_url == "https://alexeygrigorev.com/cloudflare-agent-git/subscribe"


BOTH_LISTS = (
    "pocketshell org=pocketshell client=pocketshell audience=pocketshell "
    "category=newsletter template=confirm-signup confirm_base=https://pocketshell.io/; "
    "agent-git-lab org=agent-git-lab client=agent-git-lab audience=agent-git-lab "
    "category=newsletter template=confirm-signup "
    "confirm_base=https://alexeygrigorev.com/cloudflare-agent-git/subscribe/"
)


@override_settings(RELAY_PUBLIC_LISTS=BOTH_LISTS)
def test_provision_public_signup_adds_a_separate_list_without_renaming_pocketshell(pocketshell):
    call_command("provision_public_signup")

    pocketshell["organization"].refresh_from_db()
    pocketshell["client"].refresh_from_db()
    assert pocketshell["organization"].name == "PocketShell"
    assert pocketshell["client"].sender_emails == [{"id": "hello", "email": "PocketShell <hello@pocketshell.io>"}]
    lab = Client.objects.get(slug="agent-git-lab")
    assert lab.organization.slug == "agent-git-lab"
    assert lab.organization_id != pocketshell["organization"].id
    assert Audience.objects.get(slug="agent-git-lab").organization_id == lab.organization_id
    assert lab.sender_emails == [{"id": "hello", "email": "Agent Git Lab <hello@datatalks.club>"}]
    template = EmailTemplate.objects.get(client=lab, key="confirm-signup")
    assert template.subject == "Confirm your email"
    assert template.is_active is True


SINGLE = "pocketshell org=pocketshell client=pocketshell audience=pocketshell category=newsletter template=welcome optin=single"
SINGLE_SETTINGS = dict(RELAY_PUBLIC_LISTS=SINGLE, RELAY_PUBLIC_SUBSCRIBE_ORIGINS=frozenset(ORIGINS))


@pytest.fixture
def welcome(pocketshell):
    EmailTemplate.objects.create(
        client=pocketshell["client"],
        key="welcome",
        name="Welcome",
        subject="Thanks for joining us",
        html_body="<p>Join Slack: http://join.datatalks.club/</p>",
        text_body="Join Slack: http://join.datatalks.club/",
        is_transactional=True,
        is_active=True,
    )


def post_single(django_client, payload, **extra):
    return django_client.post(
        reverse("mailing:api_public_subscribe", kwargs={"list_key": "pocketshell"}),
        data=payload,
        content_type="application/json",
        HTTP_ORIGIN="https://pocketshell.io",
        HTTP_USER_AGENT="pytest-browser",
        **extra,
    )


@override_settings(**SINGLE_SETTINGS)
def test_single_optin_subscribes_records_consent_and_sends_welcome(client, welcome, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_single(
        client, {"email": "Ada@Example.com", "page": "https://pocketshell.io/"}, HTTP_X_FORWARDED_FOR="1.2.3.4, 9.9.9.9"
    )

    assert response.status_code == 200
    assert response.json() == {"status": "subscribed"}
    assert len(enqueued) == 1
    message = TransactionalMessage.objects.get()
    assert message.template_key == "welcome"
    assert message.email.lower() == "ada@example.com"
    subscription = Subscription.objects.get()
    assert subscription.status == SubscriptionStatus.SUBSCRIBED
    assert subscription.verified_at is not None  # campaigns skip unverified contacts
    assert CategoryPreference.objects.get().enabled is True
    consent = ContactSourceMetadata.objects.get(source="website-signup").metadata
    assert consent["consent_ip"] == "1.2.3.4"
    assert consent["origin"] == "https://pocketshell.io"
    assert consent["user_agent"] == "pytest-browser"
    assert consent["page"] == "https://pocketshell.io/"
    assert consent["list"] == "pocketshell"
    assert consent["consent_at"]


@override_settings(**SINGLE_SETTINGS)
def test_single_optin_existing_subscriber_gets_no_email(client, welcome, pocketshell, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    contact = Contact.objects.create(email="ada@example.com")
    Subscription.objects.create(
        contact=contact, audience=pocketshell["audience"], client=pocketshell["client"], status=SubscriptionStatus.SUBSCRIBED
    )

    response = post_single(client, {"email": "ada@example.com"})

    assert response.json() == {"status": "already_subscribed"}
    assert enqueued == []


@override_settings(**SINGLE_SETTINGS)
def test_single_optin_never_resubscribes_unsubscribed_or_suppressed(client, welcome, pocketshell, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    gone = Contact.objects.create(email="gone@example.com")
    Subscription.objects.create(
        contact=gone, audience=pocketshell["audience"], client=pocketshell["client"], status=SubscriptionStatus.UNSUBSCRIBED
    )
    Contact.objects.create(email="bounced@example.com", hard_bounced_at=timezone.now())

    first = post_single(client, {"email": "gone@example.com"})
    second = post_single(client, {"email": "bounced@example.com"})

    assert first.json() == {"status": "not_resubscribed"}
    assert second.json() == {"status": "not_resubscribed"}
    assert enqueued == []
    assert Subscription.objects.get(contact=gone).status == SubscriptionStatus.UNSUBSCRIBED


@override_settings(**SINGLE_SETTINGS)
def test_single_optin_honeypot_looks_like_success_but_does_nothing(client, welcome, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_single(client, {"email": "bot@example.com", "company": "Acme"})

    assert response.status_code == 200
    assert response.json() == {"status": "subscribed"}
    assert enqueued == []
    assert not Contact.objects.exists()
    assert not Subscription.objects.exists()


@override_settings(**SINGLE_SETTINGS, RELAY_PUBLIC_SUBSCRIBE_IP_LIMIT=2)
def test_single_optin_limits_signups_per_ip(client, welcome, monkeypatch):
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", lambda payload: None)

    statuses = [
        post_single(client, {"email": f"u{i}@example.com"}, HTTP_X_FORWARDED_FOR="5.5.5.5").status_code
        for i in range(3)
    ]
    other_ip = post_single(client, {"email": "other@example.com"}, HTTP_X_FORWARDED_FOR="6.6.6.6")

    assert statuses == [200, 200, 429]
    assert other_ip.status_code == 200
    assert not Subscription.objects.filter(contact__normalized_email="u2@example.com").exists()


@override_settings(**SINGLE_SETTINGS)
def test_single_optin_missing_template_fails_loud_and_leaves_nobody_subscribed(client, pocketshell, caplog):
    with caplog.at_level("ERROR"):
        response = post_single(client, {"email": "ada@example.com"})

    assert response.status_code == 404
    assert not Subscription.objects.exists()
    assert "relay_public_subscribe_failed" in caplog.text


@override_settings(**SINGLE_SETTINGS)
def test_single_optin_unexpected_error_is_logged_and_returns_500(client, welcome, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("queue down")

    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", boom)
    with caplog.at_level("ERROR"):
        response = post_single(client, {"email": "ada@example.com"})

    assert response.status_code == 500
    assert response.json() == {"error": {"code": "internal_error"}}
    assert "relay_public_subscribe_failed" in caplog.text


@override_settings(**SINGLE_SETTINGS)
def test_single_optin_errors_count_in_the_transactional_monitor(client, welcome, pocketshell):
    post_single(client, {"email": "not-an-email"})

    row = TransactionalRequestMinute.objects.get(client=pocketshell["client"])
    assert (row.total, row.errors, row.server_errors) == (1, 1, 0)


def test_parse_public_lists_single_optin_needs_no_confirm_base():
    spec = parse_public_lists(SINGLE)["pocketshell"]

    assert spec.optin == "single"
    assert spec.confirm_base_url == ""
    with pytest.raises(Exception):
        parse_public_lists(SINGLE + " optin=bogus")
    assert parse_public_lists(LISTS)["pocketshell"].optin == "double"


def test_default_public_list_is_the_datatalks_newsletter():
    spec = parse_public_lists(settings.RELAY_PUBLIC_LISTS)["dtc-newsletter"]

    assert (spec.organization_slug, spec.client_slug, spec.audience_slug) == ("datatalksclub", "dtc-newsletter", "dtc")
    assert spec.optin == "single"
    assert spec.template_key == "newsletter-welcome"
    assert "https://www.datatalks.club" in settings.RELAY_PUBLIC_SUBSCRIBE_ORIGINS
