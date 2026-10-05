"""Bounded loading and query regression checks using realistic synthetic volumes."""

from time import perf_counter

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from mailing.audience_ui import audience_page_context, eligibility_summary, scoped_health_summary, scoped_tags
from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import (
    Audience,
    Campaign,
    Client,
    Contact,
    EmailTemplate,
    Organization,
    Subscription,
    TransactionalMessage,
)
from mailing.setup_views import client_setup_context

pytestmark = pytest.mark.django_db


@pytest.fixture
def scale_scope(client):
    org = Organization.objects.create(name="Scale validation", slug="scale-validation")
    integration = Client.objects.create(organization=org, name="Scale app", slug="scale-app")
    audience = Audience.objects.create(organization=org, name="Scale audience", slug="scale-audience")
    client.force_login(get_user_model().objects.create_user(username="scale-review", is_staff=True))
    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = integration.id
    session.save()
    return integration, audience


def add_members(integration, audience, start, count):
    contacts = Contact.objects.bulk_create(
        [
            Contact(
                email=f"scale-{index:05d}@example.com",
                normalized_email=f"scale-{index:05d}@example.com",
                verified_at=timezone.now(),
            )
            for index in range(start, start + count)
        ]
    )
    Subscription.objects.bulk_create(
        [
            Subscription(contact=contact, audience=audience, client=integration, status="subscribed")
            for contact in contacts
        ]
    )
    return contacts


def context_with_queries(audience, integration):
    request = RequestFactory().get(reverse("mailing:audience_detail", args=[audience.id]))
    started = perf_counter()
    with CaptureQueriesContext(connection) as captured:
        context = audience_page_context(request, audience, integration)
        list(context["breakdowns"]["tags"])
    return context, len(captured), perf_counter() - started


def test_audience_loading_stays_page_bounded_at_1000_members(scale_scope, record_property):
    integration, audience = scale_scope
    add_members(integration, audience, 0, 25)
    small, small_queries, small_seconds = context_with_queries(audience, integration)
    add_members(integration, audience, 25, 975)
    large, large_queries, large_seconds = context_with_queries(audience, integration)
    assert len(small["member_rows"]) == len(large["member_rows"]) == 25
    assert large["members"].paginator.count == 1000
    assert large_queries == small_queries
    record_property("members", 1000)
    record_property("audience_queries", large_queries)
    record_property("audience_25_seconds", round(small_seconds, 4))
    record_property("audience_1000_seconds", round(large_seconds, 4))


def test_health_and_eligibility_aggregate_without_loading_member_rows(scale_scope, django_assert_num_queries):
    integration, audience = scale_scope
    add_members(integration, audience, 0, 1000)
    with django_assert_num_queries(2):
        counts, _eligible, _excluded = eligibility_summary(audience, integration)
    assert counts["total"] == counts["eligible"] == 1000
    with django_assert_num_queries(1):
        health = scoped_health_summary(audience, integration)
    assert next(stat.value for stat in health if stat.key == "members") == 1000
    with django_assert_num_queries(1):
        assert list(scoped_tags(audience, integration)) == []


def test_activity_2000_messages_is_paginated_and_query_count_bounded(client, scale_scope, record_property):
    integration, audience = scale_scope
    contacts = add_members(integration, audience, 0, 1000)
    template = EmailTemplate.objects.create(client=integration, name="Welcome", key="welcome")
    messages = [
        TransactionalMessage(
            client=integration,
            contact=contacts[index % len(contacts)],
            template=template,
            template_key=template.key,
            email=contacts[index % len(contacts)].email,
            subject=f"Welcome {index}",
            status="sent",
        )
        for index in range(2000)
    ]
    TransactionalMessage.objects.bulk_create(messages[:25])
    url = reverse("mailing:email_activity")
    with CaptureQueriesContext(connection) as captured:
        small = client.get(url, {"type": "transactional"})
    small_queries = len(captured)
    assert small.status_code == 200
    TransactionalMessage.objects.bulk_create(messages[25:])
    started = perf_counter()
    with CaptureQueriesContext(connection) as captured:
        large = client.get(url, {"type": "transactional", "page": 2})
    assert large.status_code == 200
    assert large.context["activity"].paginator.count == 2000
    assert large.context["activity"].number == 2
    assert len(large.context["message_rows"]) == 25
    assert len(captured) == small_queries
    record_property("activity_messages", 2000)
    record_property("activity_queries", len(captured))
    record_property("activity_seconds", round(perf_counter() - started, 4))


def test_activity_selectors_bounded_and_bookmarked_selection_retained(client, scale_scope):
    integration, audience = scale_scope
    templates = EmailTemplate.objects.bulk_create(
        [
            EmailTemplate(client=integration, name=f"Template {index:03d}", key=f"template-{index}")
            for index in range(150)
        ]
    )
    campaigns = Campaign.objects.bulk_create(
        [Campaign(client=integration, audience=audience, subject=f"Campaign {index:03d}") for index in range(150)]
    )
    response = client.get(reverse("mailing:email_activity"))
    assert len(response.context["templates"]) == 100
    assert len(response.context["campaigns"]) == 100
    assert b"100 most recent campaigns" in response.content
    bookmarked = client.get(reverse("mailing:email_activity"), {"campaign": campaigns[0].pk})
    assert campaigns[0].pk in [campaign.pk for campaign in bookmarked.context["campaigns"]]
    bookmarked_template = client.get(reverse("mailing:email_activity"), {"template": templates[-1].pk})
    assert templates[-1].pk in [template.pk for template in bookmarked_template.context["templates"]]


def test_setup_readiness_reuses_key_check(scale_scope, django_assert_num_queries):
    integration, _audience = scale_scope
    with django_assert_num_queries(3):
        checks = client_setup_context(integration)["setup_checklist"]
    assert len(checks) == 4
