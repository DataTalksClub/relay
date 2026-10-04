from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import Audience, Campaign, CampaignRecipient, Client, Contact, Organization, Subscription
from mailing.services.campaigns import _skip_reason

pytestmark = pytest.mark.django_db


@pytest.fixture
def audience_scope(client):
    org = Organization.objects.create(name="Community", slug="sections-org")
    integration = Client.objects.create(organization=org, name="Courses", slug="sections-client")
    audience = Audience.objects.create(organization=org, name="Learners", slug="sections-audience")
    client.force_login(get_user_model().objects.create_user(username="sections-operator", is_staff=True))
    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = integration.id
    session.save()
    return audience, integration


def add_member(audience, integration, email, **values):
    contact = Contact.objects.create(email=email, normalized_email=email, **values)
    Subscription.objects.create(contact=contact, audience=audience, client=integration, status="subscribed")
    return contact


def test_separate_audience_screens_and_unknown_section_fallback(client, audience_scope):
    audience, _ = audience_scope
    url = reverse("mailing:audience_detail", args=[audience.id])
    ids = {
        "members": "audience-members",
        "segments": "audience-segments",
        "campaigns": "audience-campaign-history",
        "activity": "audience-activity",
        "health": "audience-health",
    }
    for section, shown in ids.items():
        response = client.get(url, {"section": section})
        assert response.status_code == 200
        html = response.content.decode()
        assert f'id="{shown}"' in html
        for other, hidden in ids.items():
            if other != section:
                assert f'id="{hidden}"' not in html
        assert response.context["section"] == section
    assert client.get(url, {"section": "unknown"}).context["section"] == "members"


def test_scoped_eligibility_counts_match_policy_and_clickable_results(client, audience_scope):
    audience, integration = audience_scope
    eligible = add_member(
        audience, integration, "eligible@example.com", verified_at=timezone.now(), email_validation_status="valid"
    )
    excluded = add_member(
        audience, integration, "excluded@example.com", verified_at=timezone.now(), hard_bounced_at=timezone.now()
    )
    override = add_member(audience, integration, "override@example.com", verified_at=timezone.now())
    Subscription.objects.create(contact=override, audience=audience, client=None, status="unsubscribed")
    other_client = Client.objects.create(organization=integration.organization, name="Other", slug="other")
    add_member(audience, other_client, "other@example.com", verified_at=timezone.now())
    campaign = Campaign(audience=audience, client=integration)
    assert not _skip_reason(eligible, campaign)
    assert _skip_reason(excluded, campaign)
    assert _skip_reason(override, campaign)
    url = reverse("mailing:audience_detail", args=[audience.id])
    response = client.get(url)
    counts = response.context["eligibility_summary"]
    assert [stat["value"] for stat in counts] == [3, 1, 2]
    for stat in counts:
        linked = client.get(stat["url"])
        assert linked.context["members"].paginator.count == stat["value"]
    filtered = client.get(url, {"eligibility": "eligible", "q": "excluded"})
    assert filtered.context["members"].paginator.count == 0


def test_filters_remove_individually_and_navigation_keeps_context(client, audience_scope):
    audience, _ = audience_scope
    url = reverse("mailing:audience_detail", args=[audience.id])
    response = client.get(
        url, {"q": "person", "include_tags": ["first", "second"], "suppression": "hard_bounced", "page": "2"}
    )
    chips = response.context["filter_chips"]
    tag = next(chip for chip in chips if chip["label"] == "Includes tag" and chip["value"] == "first")
    query = parse_qs(urlsplit(tag["url"]).query)
    assert query["include_tags"] == ["second"]
    assert query["q"] == ["person"]
    assert query["suppression"] == ["hard_bounced"]
    assert "page" not in query
    for section in response.context["sections"]:
        query = parse_qs(urlsplit(section["url"]).query)
        assert query["include_tags"] == ["first", "second"]
        assert query["q"] == ["person"]
        assert "page" not in query


def test_health_inactivity_is_selected_client_campaign_history(client, audience_scope):
    audience, integration = audience_scope
    contact = add_member(audience, integration, "inactive@example.com")
    other = Client.objects.create(organization=integration.organization, name="Other", slug="other")
    campaign = Campaign.objects.create(audience=audience, client=integration, subject="Course")
    other_campaign = Campaign.objects.create(audience=audience, client=other, subject="Other")
    CampaignRecipient.objects.create(
        campaign=campaign, contact=contact, email=contact.email, status="sent", sent_at=timezone.now()
    )
    CampaignRecipient.objects.create(
        campaign=other_campaign, contact=contact, email=contact.email, status="sent", first_opened_at=timezone.now()
    )
    response = client.get(reverse("mailing:audience_detail", args=[audience.id]), {"section": "health"})
    stats = {stat.key: stat.value for stat in response.context["summary"]}
    assert stats["inactive"] == 1
    assert stats["opened"] == 0
    assert b"campaign history for Courses" in response.content


@pytest.mark.parametrize(
    "signal,should_send",
    [
        ({"verified_at": timezone.now()}, True),
        ({"email_validation_status": "invalid_syntax", "verified_at": timezone.now()}, False),
        ({"email_validation_status": "no_mx", "verified_at": timezone.now()}, False),
        ({"email_validation_status": "disposable", "verified_at": timezone.now()}, False),
        ({"email_validation_status": "risky", "verified_at": timezone.now()}, False),
        ({"email_validation_status": "manually_invalid", "verified_at": timezone.now()}, False),
        ({"email_validation_status": "externally_validated", "verified_at": timezone.now()}, True),
        ({"global_unsubscribed_at": timezone.now(), "verified_at": timezone.now()}, False),
        ({"complained_at": timezone.now(), "verified_at": timezone.now()}, False),
        ({}, False),
    ],
)
def test_database_eligibility_agrees_with_campaign_policy(audience_scope, signal, should_send):
    from mailing.audience_ui import eligibility_summary  # noqa: PLC0415

    audience, integration = audience_scope
    contact = add_member(audience, integration, "policy@example.com", **signal)
    counts, eligible, excluded = eligibility_summary(audience, integration)
    assert (not bool(_skip_reason(contact, Campaign(audience=audience, client=integration)))) == should_send
    assert counts["eligible"] == int(should_send)
    assert eligible.count() == int(should_send)
    assert excluded.count() == int(not should_send)


def test_database_eligibility_honors_subscription_verification_and_consent(audience_scope):
    from mailing.audience_ui import eligibility_summary  # noqa: PLC0415

    audience, integration = audience_scope
    contact = add_member(audience, integration, "scope-policy@example.com")
    audience_subscription = Subscription.objects.create(
        contact=contact, audience=audience, client=None, status="subscribed", verified_at=timezone.now()
    )
    assert eligibility_summary(audience, integration)[0]["eligible"] == 1
    audience_subscription.status = "unsubscribed"
    audience_subscription.save()
    assert eligibility_summary(audience, integration)[0]["eligible"] == 0
    audience_subscription.delete()
    subscription = Subscription.objects.get(contact=contact, client=integration)
    subscription.verified_at = timezone.now()
    subscription.save()
    assert eligibility_summary(audience, integration)[0]["eligible"] == 1
    subscription.status = "pending"
    subscription.save()
    assert eligibility_summary(audience, integration)[0]["eligible"] == 0


def test_summary_query_count_does_not_grow_with_members(audience_scope, django_assert_num_queries):
    from mailing.audience_ui import eligibility_summary  # noqa: PLC0415

    audience, integration = audience_scope
    for index in range(40):
        add_member(audience, integration, f"scale-{index}@example.com", verified_at=timezone.now())
    with django_assert_num_queries(2):
        counts, eligible, excluded = eligibility_summary(audience, integration)
    assert counts == {"total": 40, "eligible": 40, "excluded": 0}
    # Returning subqueries keeps page filtering in SQL instead of loading every ID.
    assert not isinstance(eligible, list)
    assert not isinstance(excluded, list)


def test_segments_counts_and_clickthrough_are_selected_client_scoped(client, audience_scope):
    from mailing.models import ContactTag, Tag  # noqa: PLC0415

    audience, integration = audience_scope
    contact = add_member(audience, integration, "tag-member@example.com")
    other = Client.objects.create(organization=integration.organization, name="Other", slug="tags-other")
    outside = add_member(audience, other, "tag-other@example.com")
    tag = Tag.objects.create(audience=audience, name="Course", slug="course")
    ContactTag.objects.create(contact=contact, tag=tag)
    ContactTag.objects.create(contact=outside, tag=tag)
    response = client.get(reverse("mailing:audience_detail", args=[audience.id]), {"section": "segments"})
    assert response.context["breakdowns"]["tags"][0].count == 1
    linked = client.get(
        reverse("mailing:audience_detail", args=[audience.id]), {"section": "members", "include_tags": "course"}
    )
    assert linked.context["members"].paginator.count == 1


def test_pagination_preserves_filters_and_current_section(client, audience_scope):
    audience, integration = audience_scope
    for index in range(27):
        add_member(audience, integration, f"page-{index:02d}@example.com")
    response = client.get(
        reverse("mailing:audience_detail", args=[audience.id]), {"section": "members", "q": "page-", "page": 2}
    )
    assert response.context["members"].number == 2
    assert response.context["members"].paginator.count == 27
    assert len(response.context["member_rows"]) == 2
    assert "section=members" in response.context["pagination_querystring"]
    assert "q=page-" in response.context["pagination_querystring"]


def test_segment_outcome_counts_match_linked_members_after_repeated_sends(client, audience_scope):
    audience, integration = audience_scope
    contact = add_member(audience, integration, "repeat@example.com")
    for subject in ["First", "Second"]:
        campaign = Campaign.objects.create(audience=audience, client=integration, subject=subject)
        CampaignRecipient.objects.create(
            campaign=campaign, contact=contact, email=contact.email, status="sent", sent_at=timezone.now()
        )
    response = client.get(reverse("mailing:audience_detail", args=[audience.id]), {"section": "segments"})
    outcome = next(row for row in response.context["segment_links"]["campaign_statuses"] if row["label"] == "Sent")
    assert outcome["count"] == 1
    linked = client.get(reverse("mailing:audience_detail", args=[audience.id]) + outcome["url"])
    assert linked.context["members"].paginator.count == outcome["count"]
