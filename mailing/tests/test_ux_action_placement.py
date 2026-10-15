"""dakit "Action placement": create actions sit on the collection they create
into, never in the page header's corner (dakit docs/guidelines.md)."""

from html.parser import HTMLParser

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import Audience, Campaign, Client, InboundAddress, Organization

pytestmark = pytest.mark.django_db


@pytest.fixture
def operator():
    return get_user_model().objects.create_user("operator", "operator@example.com", "password", is_staff=True)


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="DataTalksClub", slug="datatalks-club")


@pytest.fixture
def client_record(organization):
    return Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class Regions(HTMLParser):
    """Collects the actions (links and buttons) inside each page header and
    each dk-collection-header, plus every data-dk-create element."""

    def __init__(self):
        super().__init__()
        self.stack = []  # (tag, region or None)
        self.page_header = []
        self.collection_headers = []  # [{"title": str, "actions": [...]}]
        self.creates = []
        self._action = None
        self._title = None

    def _region(self):
        for _tag, region in reversed(self.stack):
            if region is not None:
                return region
        return None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        region = None
        if "page-header" in classes:
            region = self.page_header
        elif "dk-collection-header" in classes:
            header = {"title": "", "actions": []}
            self.collection_headers.append(header)
            region = header
        if tag in ("a", "button"):
            self._action = {"tag": tag, "attrs": attrs, "text": "", "region": self._region()}
            if "data-dk-create" in attrs:
                self.creates.append(self._action)
        if "dk-collection-header__title" in classes:
            self._title = self.collection_headers[-1]
        if tag not in VOID:
            self.stack.append((tag, region))

    def handle_endtag(self, tag):
        if tag in ("a", "button") and self._action is not None:
            action, self._action = self._action, None
            action["text"] = " ".join(action["text"].split())
            region = action.pop("region")
            if isinstance(region, dict):
                region["actions"].append(action)
            elif region is not None:
                region.append(action)
        if tag in ("h1", "h2", "h3", "h4") and self._title is not None:
            self._title["title"] = " ".join(self._title["title"].split())
            self._title = None
        while self.stack:
            open_tag, _ = self.stack.pop()
            if open_tag == tag:
                break

    def handle_data(self, data):
        if self._action is not None:
            self._action["text"] += data
        if self._title is not None:
            self._title["title"] += data


def regions(response):
    assert response.status_code == 200
    parser = Regions()
    parser.html = response.content.decode()
    parser.feed(parser.html)
    return parser


def header_texts(parsed):
    return [action["text"] for action in parsed.page_header]


def collection(parsed, title):
    matches = [header for header in parsed.collection_headers if header["title"] == title]
    assert matches, f"no collection header titled {title!r}: {[h['title'] for h in parsed.collection_headers]}"
    return matches[0]


def assert_page_create(parsed, title, label, href):
    """The page's one create follows the collection title, is primary, and answers c."""
    header = collection(parsed, title)
    create = header["actions"][0]
    assert create["text"] == label
    assert create["attrs"]["href"] == href
    assert "dk-collection-create" in create["attrs"]["class"]
    assert "dk-button--primary" in create["attrs"]["class"]
    assert create["attrs"]["aria-keyshortcuts"] == "c"
    assert create["attrs"]["title"] == f"{label} (C)"
    assert [c["text"] for c in parsed.creates] == [label], "a page has one shortcut create"


def test_base_arms_the_create_shortcut(client, operator, client_record):
    client.force_login(operator)
    html = client.get(reverse("mailing:campaign_list")).content.decode()
    assert "dakit/dist/collection.js" in html
    assert "dakitCollection.installShortcut(document)" in html


def test_campaign_list_create_follows_the_campaigns_title(client, operator, client_record, audience):
    Campaign.objects.create(audience=audience, client=client_record, subject="Weekly update", status="sent", sent_at=timezone.now())
    client.force_login(operator)

    parsed = regions(client.get(reverse("mailing:campaign_list")))

    assert "Create campaign" not in header_texts(parsed)
    assert header_texts(parsed) == ["Audiences", "Find contact"]
    assert_page_create(parsed, "All campaigns", "Create campaign", reverse("mailing:campaign_create"))
    assert '<span class="dk-collection-header__meta">1 campaign</span>' in parsed.html


def test_campaign_list_empty_state_repeats_the_create_as_secondary(client, operator, client_record):
    client.force_login(operator)

    parsed = regions(client.get(reverse("mailing:campaign_list")))

    assert_page_create(parsed, "All campaigns", "Create campaign", reverse("mailing:campaign_create"))
    assert collection(parsed, "All campaigns")["actions"][1:] == []
    assert "No campaigns yet." in parsed.html
    assert "Create the first campaign" in parsed.html
    assert "dk-button--secondary dk-button--sm dk-collection-create" in parsed.html


def test_client_list_create_follows_the_clients_title(client, operator, client_record):
    client.force_login(operator)

    parsed = regions(client.get(reverse("mailing:client_list")))

    assert header_texts(parsed) == []
    assert_page_create(parsed, "All clients", "Create client", reverse("mailing:client_create"))
    assert '<span class="dk-collection-header__meta">1 integration</span>' in parsed.html


def test_client_list_empty_state_repeats_the_create_as_secondary(client, operator):
    client.force_login(operator)

    parsed = regions(client.get(reverse("mailing:client_list")))

    assert_page_create(parsed, "All clients", "Create client", reverse("mailing:client_create"))
    assert "No clients found." in parsed.html
    assert "Create the first client" in parsed.html


def test_audience_list_create_follows_the_audiences_title(client, operator, client_record, audience):
    client.force_login(operator)

    parsed = regions(client.get(reverse("mailing:audience_list")))

    assert header_texts(parsed) == ["Search contacts"]
    assert_page_create(parsed, "Audience Health", "Create audience", reverse("mailing:audience_create"))
    assert '<span class="dk-collection-header__meta">1 audience</span>' in parsed.html
    assert "Open members to check campaign eligibility for DTC Courses." in parsed.html


def test_audience_list_empty_state_repeats_the_create_as_secondary(client, operator, client_record):
    client.force_login(operator)

    parsed = regions(client.get(reverse("mailing:audience_list")))

    assert "Create audience" not in header_texts(parsed)
    assert_page_create(parsed, "Audience Health", "Create audience", reverse("mailing:audience_create"))
    assert "No audiences yet." in parsed.html
    assert "Create the first audience" in parsed.html


def test_receiving_addresses_create_follows_the_addresses_title(client, operator):
    InboundAddress.objects.create(local_part="support", domain="example.com")
    InboundAddress.objects.create(local_part="sales", domain="example.com")
    client.force_login(operator)

    parsed = regions(client.get(reverse("mailing:inbound_address_list")))

    assert header_texts(parsed) == ["Inbound mail"]
    assert_page_create(parsed, "Addresses", "New address", reverse("mailing:inbound_address_create"))
    assert '<span class="dk-collection-header__meta">2 addresses</span>' in parsed.html


def test_receiving_addresses_empty_state_repeats_the_create_as_secondary(client, operator):
    client.force_login(operator)

    parsed = regions(client.get(reverse("mailing:inbound_address_list")))

    assert_page_create(parsed, "Addresses", "New address", reverse("mailing:inbound_address_create"))
    assert "No addresses yet." in parsed.html
    assert "Add the first address" in parsed.html


def test_dashboard_create_campaign_follows_recent_campaign_activity(client, operator, client_record):
    client.force_login(operator)
    session = client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = client_record.id
    session.save()

    parsed = regions(client.get(reverse("mailing:dashboard")))

    assert header_texts(parsed) == ["Find a contact"]
    assert_page_create(parsed, "Recent Campaign Activity", "Create campaign", reverse("mailing:campaign_create"))
    trail = [action["text"] for action in collection(parsed, "Recent Campaign Activity")["actions"][1:]]
    assert trail == ["View campaigns"]
    assert "Create the first campaign" in parsed.html


def test_dashboard_without_a_client_creates_clients_on_the_clients_collection(client, operator, organization):
    Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")
    Client.objects.create(organization=organization, name="Newsletter", slug="newsletter")
    client.force_login(operator)

    parsed = regions(client.get(reverse("mailing:dashboard")))

    assert "Select a client" in parsed.html
    assert header_texts(parsed) == []
    assert_page_create(parsed, "Clients", "Create client", reverse("mailing:client_create"))
