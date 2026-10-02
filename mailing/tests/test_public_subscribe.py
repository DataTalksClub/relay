import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from mailing.models import (
    Audience,
    CategoryPreference,
    Client,
    Contact,
    EmailTemplate,
    Organization,
    Subscription,
    SubscriptionStatus,
    TransactionalMessage,
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
