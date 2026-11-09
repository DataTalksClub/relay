import re
from urllib.parse import parse_qs, urlparse

import pytest
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
    CmpCallback,
    Contact,
    EmailEvent,
    EmailEventType,
    Organization,
    Subscription,
    SubscriptionStatus,
)
from mailing.services.campaign_rendering import campaign_unsubscribe_headers
from mailing.services.public_urls import (
    campaign_recipient_public_urls,
    click_redirect_url,
    open_pixel_url,
    unsubscribe_url,
)
from mailing.services.tokens import ensure_campaign_recipient_tokens, get_recipient_by_tracking_token, token_hash
from mailing.services.tracking import TRANSPARENT_GIF
from mailing.tests.callback_helpers import create_callback_endpoint
from mailing.tests.task_helpers import drain_queue

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
def contact(audience, app_client):
    contact = Contact.objects.create(email="Person@example.com", verified_at=timezone.now())
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=app_client,
        status=SubscriptionStatus.SUBSCRIBED,
    )
    return contact


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
        sent_at=timezone.now(),
    )


def test_token_generation_uses_random_hashes_only_and_preserves_existing_hashes(recipient):
    first = ensure_campaign_recipient_tokens(recipient)
    recipient.refresh_from_db()
    first_tracking_hash = recipient.tracking_token_hash
    first_unsubscribe_hash = recipient.unsubscribe_token_hash

    second = ensure_campaign_recipient_tokens(recipient)
    recipient.refresh_from_db()

    assert len(first.tracking_token) >= 40
    assert len(first.unsubscribe_token) >= 40
    assert first.tracking_token != first.unsubscribe_token
    assert recipient.tracking_token_hash == token_hash(first.tracking_token)
    assert recipient.unsubscribe_token_hash == token_hash(first.unsubscribe_token)
    assert first_tracking_hash == recipient.tracking_token_hash
    assert first_unsubscribe_hash == recipient.unsubscribe_token_hash
    assert second.tracking_token is None
    assert second.unsubscribe_token is None
    assert first.tracking_token not in {recipient.tracking_token_hash, recipient.unsubscribe_token_hash}
    assert first.unsubscribe_token not in {recipient.tracking_token_hash, recipient.unsubscribe_token_hash}


def test_random_token_generation_is_not_deterministic(campaign, contact):
    first_recipient = CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=contact.email)
    second_contact = Contact.objects.create(email="other@example.com", verified_at=timezone.now())
    second_recipient = CampaignRecipient.objects.create(
        campaign=campaign, contact=second_contact, email=second_contact.email
    )

    first_tokens = ensure_campaign_recipient_tokens(first_recipient)
    second_tokens = ensure_campaign_recipient_tokens(second_recipient)

    assert first_tokens.tracking_token != second_tokens.tracking_token
    assert first_tokens.unsubscribe_token != second_tokens.unsubscribe_token


def test_hash_lookup_finds_recipient_without_persisting_raw_token(recipient):
    tokens = ensure_campaign_recipient_tokens(recipient)
    recipient.refresh_from_db()

    assert get_recipient_by_tracking_token(tokens.tracking_token) == recipient
    assert get_recipient_by_tracking_token(recipient.tracking_token_hash) is None
    assert tokens.tracking_token not in CampaignRecipient.objects.values_list("tracking_token_hash", flat=True)


@override_settings(PUBLIC_BASE_URL="https://mail.example.com/")
def test_public_url_helpers_generate_absolute_email_urls(recipient):
    destination = "https://example.com/course?a=1&b=two"
    urls = campaign_recipient_public_urls(recipient, destination)
    tracking_token = urlparse(urls["open_pixel_url"]).path.removeprefix("/t/o/").removesuffix(".gif")
    unsubscribe_token = urlparse(urls["unsubscribe_url"]).path.removeprefix("/unsubscribe/")

    assert open_pixel_url(tracking_token) == f"https://mail.example.com/t/o/{tracking_token}.gif"
    assert unsubscribe_url(unsubscribe_token) == f"https://mail.example.com/unsubscribe/{unsubscribe_token}"

    click_url = click_redirect_url(tracking_token, destination)
    parsed = urlparse(click_url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "mail.example.com"
    assert parsed.path == f"/t/c/{tracking_token}"
    assert parse_qs(parsed.query) == {"u": [destination]}

    assert urls["open_pixel_url"].endswith(f"/t/o/{tracking_token}.gif")
    assert urls["click_redirect_url"] == click_url
    assert urls["unsubscribe_url"].endswith(f"/unsubscribe/{unsubscribe_token}")


def test_public_url_helper_does_not_rotate_existing_hashes(recipient):
    tokens = ensure_campaign_recipient_tokens(recipient)
    recipient.refresh_from_db()
    tracking_hash = recipient.tracking_token_hash
    unsubscribe_hash = recipient.unsubscribe_token_hash

    with pytest.raises(ValueError):
        campaign_recipient_public_urls(recipient)

    recipient.refresh_from_db()
    assert recipient.tracking_token_hash == tracking_hash
    assert recipient.unsubscribe_token_hash == unsubscribe_hash
    assert tokens.tracking_token is not None


def test_open_pixel_records_repeated_open_events_and_unique_state_once(client, recipient):
    token = ensure_campaign_recipient_tokens(recipient).tracking_token
    url = reverse("mailing:tracking_open", args=[token])

    first = client.get(url)
    second = client.get(url)
    assert recipient.open_count == 0  # the web only enqueues
    assert drain_queue("tracking") == 2

    recipient.refresh_from_db()
    recipient.campaign.refresh_from_db()
    assert first.status_code == 200
    assert first.headers["Content-Type"] == "image/gif"
    assert first.content == TRANSPARENT_GIF
    assert "no-store" in first.headers["Cache-Control"]
    assert second.status_code == 200
    assert recipient.open_count == 2
    assert recipient.first_opened_at is not None
    assert recipient.campaign.open_count == 2
    assert recipient.campaign.unique_open_count == 1
    assert EmailEvent.objects.filter(event_type=EmailEventType.OPEN, campaign_recipient=recipient).count() == 2


def test_open_pixel_invalid_token_is_safe_noop(client, recipient):
    response = client.get(reverse("mailing:tracking_open", args=["missing-token"]))
    malformed_response = client.get(reverse("mailing:tracking_open", args=["bad-token-\u2603"]))

    recipient.refresh_from_db()
    assert response.status_code == 404
    assert malformed_response.status_code == 404
    assert response.headers["Content-Type"] == "image/gif"
    assert recipient.open_count == 0
    assert EmailEvent.objects.count() == 0


def test_click_redirect_records_repeated_clicks_and_redirects(client, recipient):
    token = ensure_campaign_recipient_tokens(recipient).tracking_token
    url = reverse("mailing:tracking_click", args=[token])
    destination = "https://example.com/path?x=1"

    first = client.get(url, {"u": destination})
    second = client.get(url, {"u": destination})
    assert drain_queue("tracking") == 2

    recipient.refresh_from_db()
    recipient.campaign.refresh_from_db()
    assert first.status_code == 302
    assert first.headers["Location"] == destination
    assert second.status_code == 302
    assert recipient.click_count == 2
    assert recipient.first_clicked_at is not None
    assert recipient.campaign.click_count == 2
    assert recipient.campaign.unique_click_count == 1
    assert list(EmailEvent.objects.values_list("event_type", "url")) == [
        (EmailEventType.CLICK, destination),
        (EmailEventType.CLICK, destination),
    ]


@override_settings(CMP_WEBHOOK_URL="https://cmp.example.com/api/datamailer/events", CMP_WEBHOOK_TOKEN="secret")
def test_tracking_open_and_click_emit_cmp_callbacks_only(client, recipient, app_client, monkeypatch):
    create_callback_endpoint(app_client)
    monkeypatch.setattr(
        "mailing.services.cmp_callbacks.transaction.on_commit",
        lambda callback: callback(),
    )
    token = ensure_campaign_recipient_tokens(recipient).tracking_token

    open_response = client.get(reverse("mailing:tracking_open", args=[token]))
    click_response = client.get(
        reverse("mailing:tracking_click", args=[token]),
        {"u": "https://example.com/path"},
    )
    drain_queue("tracking")

    assert open_response.status_code == 200
    assert click_response.status_code == 302
    # Campaign-recipient engagement stays on the CMP channel; the client
    # callback contract carries transactional deliveries only.
    assert list(CmpCallback.objects.order_by("id").values_list("event_type", flat=True)) == [
        "message.opened",
        "message.clicked",
    ]
    assert ClientCallback.objects.count() == 0


@pytest.mark.parametrize(
    "destination", ["", "mailto:person@example.com", "javascript:alert(1)", "/relative", "https://"]
)
def test_click_redirect_rejects_missing_or_unsafe_urls(client, recipient, destination):
    token = ensure_campaign_recipient_tokens(recipient).tracking_token
    response = client.get(reverse("mailing:tracking_click", args=[token]), {"u": destination})

    recipient.refresh_from_db()
    assert response.status_code == 400
    assert recipient.click_count == 0
    assert EmailEvent.objects.count() == 0


def test_click_redirect_invalid_token_does_not_mutate(client, recipient):
    response = client.get(reverse("mailing:tracking_click", args=["missing-token"]), {"u": "https://example.com"})

    assert response.status_code == 400
    assert EmailEvent.objects.count() == 0


FORBIDDEN_PUBLIC_TERMS = [
    "datamailer",
    "relay",
    "sender",
    "audience",
    "scope",
    "action required",
    "marketing preferences",
    "marketing emails",
    "DTC Courses",
    "/admin/",
    "Campaigns",
]


def assert_no_internal_terms(html):
    visible = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    visible = re.sub(r"<[^>]+>", " ", visible)
    lowered = visible.lower()
    for term in FORBIDDEN_PUBLIC_TERMS:
        assert term.lower() not in lowered, term


def test_unsubscribe_get_renders_simple_public_page_without_login(client, recipient):
    token = ensure_campaign_recipient_tokens(recipient).unsubscribe_token

    response = client.get(reverse("mailing:public_unsubscribe", args=[token]))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Unsubscribe from DataTalksClub?" in html
    assert "Person@example.com" in html
    assert html.count("<button") == 1
    assert ">Unsubscribe</button>" in html
    assert 'type="radio"' not in html
    assert 'name="scope"' not in html
    assert_no_internal_terms(html)


def test_unsubscribe_post_unsubscribes_only_the_single_audience_and_records_event(
    client, recipient, contact, audience, django_capture_on_commit_callbacks
):
    other_audience = Audience.objects.create(organization=audience.organization, name="Other List", slug="other-list")
    other_subscription = Subscription.objects.create(
        contact=contact, audience=other_audience, status=SubscriptionStatus.SUBSCRIBED
    )
    token = ensure_campaign_recipient_tokens(recipient).unsubscribe_token
    url = reverse("mailing:public_unsubscribe", args=[token])

    with django_capture_on_commit_callbacks(execute=True):
        first = client.post(url)
        second = client.post(url)
    # The suppression itself is written synchronously; only the counter refresh is queued.
    assert Subscription.objects.get(contact=contact, audience=audience, client__isnull=True).status == (
        SubscriptionStatus.UNSUBSCRIBED
    )
    drain_queue("tracking")

    recipient.refresh_from_db()
    contact.refresh_from_db()
    other_subscription.refresh_from_db()
    recipient.campaign.refresh_from_db()
    assert first.status_code == 200
    assert second.status_code == 200
    html = first.content.decode()
    assert "You're unsubscribed." in html
    assert "You won't get DataTalksClub anymore." in html
    assert "Resubscribe" in html
    assert_no_internal_terms(html)
    assert recipient.status == CampaignRecipientStatus.UNSUBSCRIBED
    assert recipient.campaign.unsubscribe_count == 1
    events = EmailEvent.objects.filter(event_type=EmailEventType.UNSUBSCRIBE, campaign_recipient=recipient)
    assert events.count() == 2
    assert all(event.metadata == {"scope": "audience"} for event in events)

    subscription = Subscription.objects.get(contact=contact, audience=audience, client__isnull=True)
    assert subscription.status == SubscriptionStatus.UNSUBSCRIBED
    assert subscription.unsubscribe_reason == "public_unsubscribe"
    # Nothing outside this one list changes.
    assert contact.global_unsubscribed_at is None
    assert other_subscription.status == SubscriptionStatus.SUBSCRIBED
    assert Subscription.objects.get(contact=contact, audience=audience, client=recipient.campaign.client).status == (
        SubscriptionStatus.SUBSCRIBED
    )


def test_unsubscribe_ignores_legacy_scope_field(client, recipient, contact):
    token = ensure_campaign_recipient_tokens(recipient).unsubscribe_token

    response = client.post(reverse("mailing:public_unsubscribe", args=[token]), {"scope": "global"})

    contact.refresh_from_db()
    assert response.status_code == 200
    assert contact.global_unsubscribed_at is None


def test_resubscribe_restores_only_the_single_audience(client, recipient, contact, audience):
    token = ensure_campaign_recipient_tokens(recipient).unsubscribe_token
    url = reverse("mailing:public_unsubscribe", args=[token])
    client.post(url)

    response = client.post(url, {"action": "resubscribe"})

    contact.refresh_from_db()
    subscription = Subscription.objects.get(contact=contact, audience=audience, client__isnull=True)
    assert response.status_code == 200
    assert "You're subscribed again." in response.content.decode()
    assert subscription.status == SubscriptionStatus.SUBSCRIBED
    assert contact.global_unsubscribed_at is None


@override_settings(CMP_WEBHOOK_URL="https://cmp.example.com/api/datamailer/events", CMP_WEBHOOK_TOKEN="secret")
def test_unsubscribe_post_emits_cmp_callback_only(client, recipient, app_client, monkeypatch):
    create_callback_endpoint(app_client)
    monkeypatch.setattr(
        "mailing.services.cmp_callbacks.transaction.on_commit",
        lambda callback: callback(),
    )
    token = ensure_campaign_recipient_tokens(recipient).unsubscribe_token

    response = client.post(
        reverse("mailing:public_unsubscribe", args=[token]),
    )

    assert response.status_code == 200
    # A campaign unsubscribe link is a CMP-channel contact event; the client
    # callback contract carries client-level subscription changes made
    # through the subscription APIs.
    callback = ClientCallback.objects.filter(status=ClientCallbackStatus.PENDING).first()
    assert callback is None
    cmp_body = CmpCallback.objects.order_by("-id").first()
    assert cmp_body is not None and cmp_body.event_type == "subscription.unsubscribed"


def test_rfc8058_one_click_post_unsubscribes_single_audience_without_form(client, recipient, contact, audience):
    token = ensure_campaign_recipient_tokens(recipient).unsubscribe_token

    response = client.post(
        reverse("mailing:public_unsubscribe", args=[token]),
        {"List-Unsubscribe": "One-Click"},
    )

    contact.refresh_from_db()
    recipient.refresh_from_db()
    subscription = Subscription.objects.get(contact=contact, audience=audience, client__isnull=True)
    sender_subscription = Subscription.objects.get(contact=contact, audience=audience, client=recipient.campaign.client)
    event = EmailEvent.objects.get(campaign_recipient=recipient, event_type=EmailEventType.UNSUBSCRIBE)
    assert response.status_code == 204
    assert recipient.status == CampaignRecipientStatus.UNSUBSCRIBED
    assert subscription.status == SubscriptionStatus.UNSUBSCRIBED
    assert sender_subscription.status == SubscriptionStatus.SUBSCRIBED
    assert contact.global_unsubscribed_at is None
    assert event.metadata == {"scope": "audience"}


def test_unsubscribe_invalid_token_does_not_mutate(client, recipient):
    invalid_token_response = client.post(reverse("mailing:public_unsubscribe", args=["missing-token"]))
    html = invalid_token_response.content.decode()

    recipient.refresh_from_db()
    assert invalid_token_response.status_code == 404
    assert "This unsubscribe link is no longer available" in html
    assert "Person@example.com" not in html
    assert_no_internal_terms(html)
    assert recipient.status == CampaignRecipientStatus.SENT
    assert EmailEvent.objects.count() == 0


@override_settings(PUBLIC_BASE_URL="https://mail.example.com")
def test_campaign_headers_include_per_list_list_id_with_one_click(campaign):
    headers = campaign_unsubscribe_headers("tok", campaign)

    assert headers["List-Id"] == "DataTalksClub <dtc-courses.datatalks-club.list-id.mail.example.com>"
    assert headers["List-Unsubscribe"] == "<https://mail.example.com/unsubscribe/tok>"
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


@override_settings(PUBLIC_BASE_URL="https://mail.example.com")
def test_list_id_differs_per_client_and_audience(campaign, organization):
    other_audience = Audience.objects.create(organization=organization, name='Other "List"', slug="other")
    other = Campaign.objects.create(audience=other_audience, client=campaign.client, subject="x")

    first = campaign_unsubscribe_headers("tok", campaign)["List-Id"]
    second = campaign_unsubscribe_headers("tok", other)["List-Id"]

    assert first != second
    assert second == "Other List <dtc-courses.other.list-id.mail.example.com>"
