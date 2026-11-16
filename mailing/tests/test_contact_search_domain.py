"""Contact lookup by email substring and domain (API + operator UI).

On PostgreSQL the domain lookup must be served by the
``contacts_norm_email_rev_idx`` functional index (see migration 0060).
"""

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.urls import reverse

from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import Audience, Client, Contact, Organization, Subscription, SubscriptionStatus
from mailing.services.auth import create_client_api_key
from mailing.services.contact_email_search import filter_email_domain

pytestmark = pytest.mark.django_db

API_KEY = "contact-search-key"
EMAILS = [
    "Ann@FSU.edu",
    "bob@cs.fsu.edu",
    "carol@notfsu.edu",
    "dave@fsu.edu.au",
    "erin@netcup.de",
    "frank@example.com",
]


@pytest.fixture
def scope():
    org = Organization.objects.create(name="DataTalksClub", slug="dtc")
    audience = Audience.objects.create(organization=org, name="Newsletter", slug="newsletter")
    api_client = Client.objects.create(organization=org, name="News", slug="news")
    create_client_api_key(client=api_client, name="search", raw_api_key=API_KEY)
    for email in EMAILS:
        contact = Contact.objects.create(email=email)
        Subscription.objects.create(
            contact=contact, audience=audience, client=api_client, status=SubscriptionStatus.SUBSCRIBED
        )
    # A contact in the domain but outside the authenticated client's scope.
    other_org = Organization.objects.create(name="Other", slug="other")
    other_audience = Audience.objects.create(organization=other_org, name="Other", slug="newsletter")
    other_client = Client.objects.create(organization=other_org, name="Other", slug="other")
    outsider = Contact.objects.create(email="outsider@fsu.edu")
    Subscription.objects.create(contact=outsider, audience=other_audience, client=other_client)
    return audience, api_client


def api_get(django_client, audience, api_client, **params):
    return django_client.get(
        reverse("mailing:api_contacts"),
        {"audience": audience.slug, "client": api_client.slug, **params},
        HTTP_AUTHORIZATION=f"Bearer {API_KEY}",
    )


def emails(response):
    assert response.status_code == 200, response.content
    return sorted(contact["email"] for contact in response.json()["contacts"])


def test_api_domain_matches_domain_and_subdomains_only(client, scope):
    audience, api_client = scope
    assert emails(api_get(client, audience, api_client, domain="fsu.edu")) == ["ann@fsu.edu", "bob@cs.fsu.edu"]
    # Leading @ and case are tolerated.
    assert emails(api_get(client, audience, api_client, domain="@FSU.EDU")) == ["ann@fsu.edu", "bob@cs.fsu.edu"]


def test_api_email_contains_is_case_insensitive_substring(client, scope):
    audience, api_client = scope
    assert emails(api_get(client, audience, api_client, email_contains="NETCUP")) == ["erin@netcup.de"]
    assert emails(api_get(client, audience, api_client, q="fsu.edu")) == [
        "ann@fsu.edu",
        "bob@cs.fsu.edu",
        "carol@notfsu.edu",
        "dave@fsu.edu.au",
    ]


def test_api_search_combines_with_scope_and_cursor(client, scope):
    audience, api_client = scope
    first = api_get(client, audience, api_client, domain="fsu.edu", limit="1").json()
    assert first["count"] == 1 and first["next_cursor"]
    second = api_get(client, audience, api_client, domain="fsu.edu", cursor=first["next_cursor"]).json()
    found = {first["contacts"][0]["email"], *(c["email"] for c in second["contacts"])}
    assert found == {"ann@fsu.edu", "bob@cs.fsu.edu"}  # never the other client's outsider@fsu.edu


def test_api_rejects_invalid_domain(client, scope):
    audience, api_client = scope
    response = api_get(client, audience, api_client, domain="a b@c")
    assert response.status_code == 400
    assert response.json()["error"]["fields"]["domain"] == "invalid"


def test_csv_export_honours_domain_filter(client, scope):
    audience, api_client = scope
    response = client.get(
        reverse("mailing:api_contacts_csv"),
        {"audience": audience.slug, "client": api_client.slug, "domain": "netcup.de"},
        HTTP_AUTHORIZATION=f"Bearer {API_KEY}",
    )
    rows = response.content.decode().strip().splitlines()
    assert len(rows) == 2 and rows[1].startswith("erin@netcup.de,")


@pytest.fixture
def operator(client, scope):
    _audience, api_client = scope
    user = get_user_model().objects.create_user("operator", is_staff=True)
    client.force_login(user)
    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = api_client.id
    session.save()
    return client


def ui_emails(response):
    assert response.status_code == 200
    return sorted(row.contact.email for row in response.context["contact_rows"])


def test_ui_domain_filter_and_chip(operator):
    response = operator.get(reverse("mailing:contact_search"), {"domain": "@fsu.edu"})
    assert ui_emails(response) == ["Ann@FSU.edu", "bob@cs.fsu.edu"]
    assert {"label": "Domain", "value": "fsu.edu"} in [
        {"label": chip["label"], "value": chip["value"]} for chip in response.context["active_filters"]
    ]
    assert 'name="domain"' in response.content.decode()


def test_ui_email_contains_matches_any_case(operator):
    response = operator.get(reverse("mailing:contact_search"), {"q": "ANN@fsu"})
    assert ui_emails(response) == ["Ann@FSU.edu"]


def test_ui_ignores_malformed_domain(operator):
    response = operator.get(reverse("mailing:contact_search"), {"domain": "bad domain"})
    assert len(ui_emails(response)) == len(EMAILS)


@pytest.mark.skipif(connection.vendor != "postgresql", reason="index plan is PostgreSQL-specific")
def test_postgres_domain_lookup_uses_reverse_index(scope):
    # Six rows make any plan cheap, so steer away from full scans to show the
    # suffix predicate is index-matchable at all (at prod volume the planner
    # picks it on its own for selective domains).
    queryset = filter_email_domain(Contact.objects.order_by(), "fsu.edu")
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute("SET LOCAL enable_indexscan = off")
        plan = queryset.explain()
    assert "contacts_norm_email_rev_idx" in plan, plan
