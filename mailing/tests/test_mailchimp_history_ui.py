from datetime import UTC, datetime
from uuid import uuid4

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import Audience, Campaign, CampaignStatus, Client, Contact, EmailEvent, EmailEventType, Organization

pytestmark = pytest.mark.django_db
AT = datetime(2025, 12, 15, 9, 12, 34, tzinfo=UTC)
DC, LIST_ID, EMAIL_ID = "us19", "list-one", "source-contact-one"


@pytest.fixture
def scope(client, django_user_model):
    organization = Organization.objects.create(name="History owner", slug="history-owner")
    tenant = Client.objects.create(organization=organization, name="Newsletter", slug="newsletter")
    audience = Audience.objects.create(organization=organization, name="Source audience", slug="source-audience")
    campaign = Campaign.objects.create(client=tenant, audience=audience, subject="Archived campaign",
                                       status=CampaignStatus.SENT)
    operator = django_user_model.objects.create_user(username="history-operator", is_staff=True)
    client.force_login(operator)
    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = tenant.pk
    session.save()
    return {"client": tenant, "audience": audience, "campaign": campaign, "operator": operator,
            "organization": organization}


def record(scope, *, email_id=EMAIL_ID, campaign=None, contact=None, status="unlinked", url="https://example.com/history"):
    campaign = campaign or scope["campaign"]
    event = EmailEvent.objects.create(
        client=campaign.client, audience=campaign.audience, campaign=campaign, contact=contact,
        event_type=EmailEventType.CLICK, url=url, provider_event_id="mailchimp:click:" + uuid4().hex,
        metadata={"provider": "mailchimp", "historical_import": True, "mailchimp": {
            "datacenter": DC, "list_id": LIST_ID, "email_id": email_id, "identity_status": status,
            "campaign_id": "source-campaign", "occurrence": 1, "timestamp": AT.isoformat(),
            "archive_manifest_sha256": "a" * 64, "raw_file": "raw/source-page.json",
            "raw_member_index": 7, "raw_activity_index": 3,
        }},
    )
    EmailEvent.objects.filter(pk=event.pk).update(created_at=AT)
    return event


def detail(scope, *, audience=None, email_id=EMAIL_ID):
    return reverse("mailing:mailchimp_source_history", args=[(audience or scope["audience"]).pk, DC, LIST_ID, email_id])


def test_unlinked_source_list_groups_clicks_and_defaults_to_unlinked(client, scope):
    record(scope)
    record(scope)
    second = Campaign.objects.create(client=scope["client"], audience=scope["audience"],
                                     subject="Second archived campaign", status=CampaignStatus.SENT)
    record(scope, campaign=second, url="https://example.com/second")
    contact = Contact.objects.create(email="known-relay@example.com")
    record(scope, email_id="matched-source", contact=contact, status="matched")

    response = client.get(reverse("mailing:mailchimp_source_contacts"))

    assert response.status_code == 200
    rows = list(response.context["identities"])
    assert len(rows) == 1
    assert (rows[0]["click_count"], rows[0]["campaign_count"], rows[0]["link_count"]) == (3, 2, 2)
    assert rows[0]["source_email_id"] == EMAIL_ID
    assert rows[0]["linkage_label"] == "Unlinked Mailchimp contact"
    assert detail(scope) in response.content.decode()
    assert "matched-source" not in response.content.decode()
    assert "known-relay@example.com" not in response.content.decode()


def test_source_detail_preserves_history_and_provenance_without_identity_controls(client, scope):
    record(scope, url="https://example.com/lesson?x=1&y=2")
    before = (Contact.objects.count(), EmailEvent.objects.count())

    response = client.get(detail(scope))

    assert response.status_code == 200
    html = response.content.decode()
    assert "Unlinked Mailchimp contact" in html
    assert "Archived campaign" in html
    assert "https://example.com/lesson?x=1&amp;y=2" in html
    assert timezone.localtime(AT).strftime("%Y-%m-%d %H:%M:%S") in html
    assert AT.isoformat() in html
    assert "a" * 64 in html
    assert "raw/source-page.json" in html
    assert "source-campaign" in html
    assert response.context["summary"]["click_count"] == 1
    assert response.context["summary"]["native_contact_email"] is None
    assert 'action="/contacts/' not in html
    assert "Update subscription" not in html
    assert 'href="https://example.com/lesson' not in html
    assert (Contact.objects.count(), EmailEvent.objects.count()) == before


def test_all_linkages_only_links_explicitly_matched_relay_contact(client, scope):
    contact = Contact.objects.create(email="verified-match@example.com")
    record(scope, contact=contact, status="matched")
    record(scope, email_id="inconsistent-source", contact=contact, status="unlinked")
    native_url = reverse("mailing:contact_detail", args=[contact.normalized_email])

    listing = client.get(reverse("mailing:mailchimp_source_contacts"), {"linkage": "all"})
    assert listing.status_code == 200
    assert listing.context["identities"].paginator.count == 2
    response = client.get(detail(scope))
    assert response.context["summary"]["linkage_label"] == "Matched Relay contact"
    assert native_url in response.content.decode()
    inconsistent = client.get(detail(scope, email_id="inconsistent-source"))
    assert inconsistent.context["summary"]["native_contact_email"] is None
    assert native_url not in inconsistent.content.decode()


def test_same_source_ids_are_isolated_between_clients_and_audiences(client, scope):
    record(scope)
    other_client = Client.objects.create(organization=scope["organization"], name="Other client", slug="other")
    other_audience = Audience.objects.create(organization=scope["organization"], name="Other audience", slug="other")
    other_campaign = Campaign.objects.create(client=other_client, audience=other_audience, subject="Other client secret")
    record(scope, campaign=other_campaign, url="https://private.example/other-client")
    local_other = Campaign.objects.create(client=scope["client"], audience=other_audience, subject="Other audience history")
    record(scope, campaign=local_other, url="https://private.example/other-audience")

    response = client.get(detail(scope))
    assert response.status_code == 200
    assert response.context["summary"]["click_count"] == 1
    assert "Other audience history" not in response.content.decode()
    assert "Other client secret" not in response.content.decode()
    listing = client.get(reverse("mailing:mailchimp_source_contacts"))
    assert listing.context["identities"].paginator.count == 2
    assert "Other client secret" not in listing.content.decode()
    same_audience = client.get(detail(scope, audience=other_audience))
    assert same_audience.context["summary"]["click_count"] == 1
    assert "Other audience history" in same_audience.content.decode()
    assert "Other client secret" not in same_audience.content.decode()

    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = other_client.pk
    session.save()
    assert client.get(detail(scope)).status_code == 404


def test_malformed_cross_organization_or_campaign_scope_is_not_exposed(client, scope):
    organization = Organization.objects.create(name="Other organization", slug="other-org")
    audience = Audience.objects.create(organization=organization, name="Private audience", slug="private")
    event = record(scope)
    EmailEvent.objects.filter(pk=event.pk).update(audience=audience)
    assert client.get(detail(scope, audience=audience)).status_code == 404
    assert client.get(reverse("mailing:mailchimp_source_contacts")).context["identities"].paginator.count == 0


def test_history_is_staff_only_and_requires_selected_client(client, scope, django_user_model):
    record(scope)
    paths = [reverse("mailing:mailchimp_source_contacts"), detail(scope)]
    client.logout()
    for path in paths:
        assert client.get(path).status_code == 302
    reader = django_user_model.objects.create_user(username="nonstaff-history-reader")
    client.force_login(reader)
    for path in paths:
        assert client.get(path).status_code == 302
    client.force_login(scope["operator"])
    Client.objects.create(organization=scope["organization"], name="Second", slug="second")
    session = client.session
    session.pop(ACTIVE_CLIENT_SESSION_KEY, None)
    session.save()
    for path in paths:
        response = client.get(path)
        assert response.status_code == 302
        assert response.url == reverse("mailing:dashboard")


def test_history_rejects_mutating_http_methods(client, scope):
    record(scope)
    before = EmailEvent.objects.count()
    for path in [reverse("mailing:mailchimp_source_contacts"), detail(scope)]:
        assert client.post(path, {"identity_status": "matched"}).status_code == 405
        assert client.delete(path).status_code == 405
    assert EmailEvent.objects.count() == before


def test_literal_urls_are_escaped_and_never_activated(client, scope):
    record(scope, url='javascript:alert("<script>")')
    html = client.get(detail(scope)).content.decode()
    assert "javascript:alert(&quot;&lt;script&gt;&quot;)" in html
    assert 'href="javascript:' not in html
    assert '<script>"' not in html


@pytest.mark.parametrize("flag", ["erased", "source_history_erased"])
def test_erased_history_is_hidden_even_if_source_ids_remain(client, scope, flag):
    event = record(scope)
    event.metadata[flag] = True
    event.save(update_fields=["metadata"])
    assert client.get(detail(scope)).status_code == 404
    listing = client.get(reverse("mailing:mailchimp_source_contacts"), {"linkage": "all"})
    assert listing.context["identities"].paginator.count == 0


@pytest.mark.parametrize("field", ["datacenter", "list_id", "email_id"])
def test_incomplete_source_ids_are_not_listed(client, scope, field):
    event = record(scope)
    del event.metadata["mailchimp"][field]
    event.save(update_fields=["metadata"])
    assert client.get(detail(scope)).status_code == 404
    listing = client.get(reverse("mailing:mailchimp_source_contacts"), {"linkage": "all"})
    assert listing.context["identities"].paginator.count == 0


@pytest.mark.parametrize("field,value", [
    ("datacenter", None), ("datacenter", "invalid"), ("list_id", None),
    ("list_id", ""), ("email_id", None), ("email_id", "invalid/member"),
])
def test_malformed_source_ids_do_not_break_history_navigation(client, scope, field, value):
    event = record(scope)
    event.metadata["mailchimp"][field] = value
    event.save(update_fields=["metadata"])
    response = client.get(reverse("mailing:mailchimp_source_contacts"), {"linkage": "all"})
    assert response.status_code == 200
    assert response.context["identities"].paginator.count == 0


def test_source_identity_and_event_pagination_are_bounded(client, scope):
    for index in range(51):
        record(scope, email_id=f"member-{index:03d}")
    listing = client.get(reverse("mailing:mailchimp_source_contacts"), {"linkage": "all"})
    assert len(listing.context["identities"]) == 50
    second = client.get(reverse("mailing:mailchimp_source_contacts"), {"linkage": "all", "page": 2})
    assert len(second.context["identities"]) == 1
    assert "linkage=all" in second.content.decode()
    for _ in range(51):
        record(scope)
    response = client.get(detail(scope))
    assert len(response.context["events"]) == 50
    response = client.get(detail(scope), {"page": 2})
    assert len(response.context["events"]) == 1


def test_campaign_and_link_summaries_have_independent_pagination(client, scope):
    for index in range(51):
        campaign = Campaign.objects.create(client=scope["client"], audience=scope["audience"],
                                           subject=f"Campaign {index:03d}", status=CampaignStatus.SENT)
        record(scope, campaign=campaign, url=f"https://example.com/destination-{index:03d}")
    response = client.get(detail(scope), {"campaign_page": 2, "link_page": 2, "page": 2})
    assert response.status_code == 200
    assert len(response.context["campaigns"]) == 1
    assert len(response.context["links"]) == 1
    assert len(response.context["events"]) == 1
    assert "campaign_page=2" in response.content.decode()
    assert "link_page=2" in response.content.decode()


def test_detail_queries_do_not_grow_per_event(client, scope):
    record(scope)
    with CaptureQueriesContext(connection) as small:
        assert client.get(detail(scope)).status_code == 200
    for index in range(60):
        record(scope, url=f"https://example.com/lesson-{index}")
    with CaptureQueriesContext(connection) as large:
        assert client.get(detail(scope)).status_code == 200
    assert len(large) <= len(small) + 1
    assert len(large) <= 20
