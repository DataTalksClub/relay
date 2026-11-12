import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    Organization,
)
from mailing.services.auth import create_client_api_key

pytestmark = pytest.mark.django_db

API_KEY = "test-link-stats-key"
CAMPAIGN_KEY = "cmp-registration-production-ai-agents-generative-media-10ac3865"


@pytest.fixture
def campaign_scope():
    organization = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    client = Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")
    audience = Audience.objects.create(organization=organization, name="Courses", slug="courses")
    campaign = Campaign.objects.create(
        client=client,
        audience=audience,
        external_key=CAMPAIGN_KEY,
        subject="Registration announcement",
        status=CampaignStatus.SENT,
        recipient_count=3,
        sent_count=3,
    )
    create_client_api_key(client=client, name="Link stats test", raw_api_key=API_KEY)
    return {"organization": organization, "client": client, "audience": audience, "campaign": campaign}


def record_click(campaign_scope, email, url, event_type=EmailEventType.CLICK):
    contact, _ = Contact.objects.get_or_create(
        normalized_email=email.lower(),
        defaults={"email": email},
    )
    recipient, _ = CampaignRecipient.objects.get_or_create(
        campaign=campaign_scope["campaign"],
        contact=contact,
        defaults={"email": email},
    )
    return EmailEvent.objects.create(
        campaign=campaign_scope["campaign"],
        campaign_recipient=recipient,
        contact=contact,
        client=campaign_scope["client"],
        audience=campaign_scope["audience"],
        event_type=event_type,
        url=url,
    )


def test_campaign_link_stats_api_groups_clicks_and_counts_unique_recipients(client, campaign_scope):
    first = "https://example.com/course"
    second = "https://example.com/syllabus"
    record_click(campaign_scope, "one@example.com", first)
    record_click(campaign_scope, "one@example.com", first)
    record_click(campaign_scope, "two@example.com", first)
    record_click(campaign_scope, "two@example.com", second)
    record_click(campaign_scope, "three@example.com", "", EmailEventType.CLICK)
    record_click(campaign_scope, "three@example.com", second, EmailEventType.OPEN)

    response = client.get(
        reverse("mailing:api_campaign_links", args=[CAMPAIGN_KEY]),
        HTTP_AUTHORIZATION=f"Bearer {API_KEY}",
    )

    assert response.status_code == 200
    assert response.json() == {
        "campaign": CAMPAIGN_KEY,
        "links": [
            {"url": first, "total_clicks": 3, "unique_clickers": 2},
            {"url": second, "total_clicks": 1, "unique_clickers": 1},
        ],
    }


def test_link_stats_count_historical_contacts_and_legacy_recipient_events(client, campaign_scope):
    url = "https://example.com/history"
    legacy = record_click(campaign_scope, "one@example.com", url)
    # Older tracking rows can identify the contact through their recipient only.
    EmailEvent.objects.filter(pk=legacy.pk).update(contact=None)
    for email in ("one@example.com", "two@example.com", "two@example.com"):
        contact = Contact.objects.get_or_create(email=email, normalized_email=email)[0]
        EmailEvent.objects.create(
            campaign=campaign_scope["campaign"], contact=contact,
            client=campaign_scope["client"], audience=campaign_scope["audience"],
            event_type=EmailEventType.CLICK, url=url,
        )

    response = client.get(
        reverse("mailing:api_campaign_links", args=[CAMPAIGN_KEY]),
        HTTP_AUTHORIZATION=f"Bearer {API_KEY}",
    )

    assert response.status_code == 200
    assert response.json()["links"] == [{"url": url, "total_clicks": 4, "unique_clickers": 2}]


def test_link_stats_include_exact_unlinked_source_identities(client, campaign_scope):
    url = "https://example.com/source-history"
    legacy = record_click(campaign_scope, "one@example.com", url)
    EmailEvent.objects.filter(pk=legacy.pk).update(contact=None)
    record_click(campaign_scope, "one@example.com", url)
    identities = [
        ("us19", "list-one", "member-one"),
        ("us19", "list-one", "member-one"),
        ("us19", "list-one", "member-two"),
        ("us19", "list-two", "member-one"),
        ("us20", "list-one", "member-one"),
    ]
    for datacenter, list_id, email_id in identities:
        EmailEvent.objects.create(
            campaign=campaign_scope["campaign"], client=campaign_scope["client"],
            audience=campaign_scope["audience"], event_type=EmailEventType.CLICK, url=url,
            metadata={"provider": "mailchimp", "historical_import": True, "mailchimp": {
                "datacenter": datacenter, "list_id": list_id, "email_id": email_id,
                "identity_status": "unlinked",
            }},
        )

    response = client.get(
        reverse("mailing:api_campaign_links", args=[CAMPAIGN_KEY]),
        HTTP_AUTHORIZATION=f"Bearer {API_KEY}",
    )

    assert response.status_code == 200
    assert response.json()["links"] == [{"url": url, "total_clicks": 7, "unique_clickers": 5}]


@pytest.mark.parametrize("field,value", [
    ("datacenter", None), ("datacenter", ""), ("list_id", None),
    ("list_id", ""), ("email_id", None), ("email_id", ""), ("email_id", "invalid/member"),
])
def test_link_stats_do_not_invent_identities_from_incomplete_metadata(client, campaign_scope, field, value):
    source = {"datacenter": "us19", "list_id": "list-one", "email_id": "member-one"}
    source[field] = value
    EmailEvent.objects.create(
        campaign=campaign_scope["campaign"], client=campaign_scope["client"],
        audience=campaign_scope["audience"], event_type=EmailEventType.CLICK,
        url="https://example.com/incomplete", metadata={
            "provider": "mailchimp", "historical_import": True, "mailchimp": source,
        },
    )
    response = client.get(
        reverse("mailing:api_campaign_links", args=[CAMPAIGN_KEY]),
        HTTP_AUTHORIZATION=f"Bearer {API_KEY}",
    )
    assert response.status_code == 200
    assert response.json()["links"] == [{"url": "https://example.com/incomplete", "total_clicks": 1, "unique_clickers": 0}]


def test_campaign_link_stats_api_requires_authentication_and_hides_other_clients(client, campaign_scope):
    path = reverse("mailing:api_campaign_links", args=[CAMPAIGN_KEY])
    assert client.get(path).status_code == 401

    other = Client.objects.create(
        organization=campaign_scope["organization"], name="Other", slug="other-client"
    )
    create_client_api_key(client=other, name="Other key", raw_api_key="other-client-key")
    response = client.get(path, HTTP_AUTHORIZATION="Bearer other-client-key")
    assert response.status_code == 404
    assert response.json()["error"]["fields"] == {"external_key": "not_found"}


def test_campaign_dashboard_loads_link_stats_with_active_client_scope(client, campaign_scope):
    staff = get_user_model().objects.create_user(
        username="operator", email="operator@example.com", password="password", is_staff=True
    )
    client.force_login(staff)
    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = campaign_scope["client"].pk
    session.save()

    response = client.get(reverse("mailing:campaign_detail", args=[campaign_scope["campaign"].pk]))

    assert response.status_code == 200
    assert b"Clicks per link" in response.content
    assert b"Total clicks" in response.content
    assert b"Unique clickers" in response.content
    assert b"timed out after 10 seconds" in response.content
    assert b"window.setTimeout" in response.content
    assert b"controller.abort()" in response.content
    assert b"Retry" in response.content
    assert reverse("mailing:campaign_links_json", args=[campaign_scope["campaign"].pk]).encode() in response.content


def test_dashboard_link_stats_json_is_client_scoped(client, campaign_scope):
    staff = get_user_model().objects.create_user(
        username="operator", email="operator@example.com", password="password", is_staff=True
    )
    client.force_login(staff)
    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = campaign_scope["client"].pk
    session.save()

    response = client.get(reverse("mailing:campaign_links_json", args=[campaign_scope["campaign"].pk]))

    assert response.status_code == 200
    assert response.json() == {"campaign": CAMPAIGN_KEY, "links": []}

    other_client = Client.objects.create(
        organization=campaign_scope["organization"], name="Other", slug="other-client"
    )
    other_campaign = Campaign.objects.create(
        client=other_client,
        audience=campaign_scope["audience"],
        external_key="other-campaign",
        subject="Private to another client",
    )
    hidden = client.get(reverse("mailing:campaign_links_json", args=[other_campaign.pk]))
    assert hidden.status_code == 404
