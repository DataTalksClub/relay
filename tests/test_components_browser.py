"""Browser tests for dakit components in relay: dk-avatar and dk-tag-input."""

import pytest
from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY, get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.urls import reverse
from django.utils import timezone

from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import Audience, Client, Contact, ContactTag, Organization, Subscription, Tag
from tests.test_shell_browser import DESKTOP

playwright_api = pytest.importorskip("playwright.sync_api")

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as playwright:
        try:
            chromium = playwright.chromium.launch()
        except Exception as error:  # noqa: BLE001
            pytest.skip(f"Chromium is not installed for Playwright: {error}")
        yield chromium
        chromium.close()


def open_page_with_client(browser_instance, live_server, user, path, *, active_client_id=None, viewport=DESKTOP):
    session = SessionStore()
    session[SESSION_KEY] = str(user.pk)
    session[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    if active_client_id:
        session[ACTIVE_CLIENT_SESSION_KEY] = active_client_id
    session.save()
    context = browser_instance.new_context(viewport=viewport)
    context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": session.session_key, "url": live_server.url}])
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(live_server.url + path)
    return page, errors


@pytest.fixture
def component_data():
    org = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    c1 = Client.objects.create(organization=org, name="Courses", slug="courses")
    c2 = Client.objects.create(organization=org, name="Events", slug="events")
    user = get_user_model().objects.create_user(
        "grace", "grace@example.com", "password", is_staff=True, first_name="Grace", last_name="Hopper"
    )
    audience = Audience.objects.create(organization=org, name="Students", slug="students")
    t1 = Tag.objects.create(audience=audience, name="ml-zoomcamp", slug="ml-zoomcamp")
    t2 = Tag.objects.create(audience=audience, name="de-zoomcamp", slug="de-zoomcamp")
    t3 = Tag.objects.create(audience=audience, name="llm-zoomcamp", slug="llm-zoomcamp")
    t4 = Tag.objects.create(audience=audience, name="alumni", slug="alumni")

    contact = Contact.objects.create(
        email="student@example.com", normalized_email="student@example.com", verified_at=timezone.now()
    )
    Subscription.objects.create(contact=contact, audience=audience, client=c1, status="subscribed")
    ContactTag.objects.create(contact=contact, tag=t1)

    return {"user": user, "clients": [c1, c2], "audience": audience, "tags": [t1, t2, t3, t4]}


def test_client_switcher_renders_canonical_avatars(browser, live_server, component_data):
    user = component_data["user"]
    c1 = component_data["clients"][0]

    page, errors = open_page_with_client(browser, live_server, user, "/", active_client_id=c1.id, viewport=DESKTOP)
    brand = page.locator("button.dk-brand")
    brand.click()

    menu = page.locator("#dk-brand-menu")
    assert menu.is_visible()

    items = menu.locator(".dk-menu__item")
    assert items.count() == 2

    # Check first client avatar
    first_avatar = items.nth(0).locator(".dk-avatar.dk-avatar--xs")
    assert first_avatar.is_visible()
    assert first_avatar.locator(".dk-avatar__initials").text_content() == "C"

    # Check second client avatar
    second_avatar = items.nth(1).locator(".dk-avatar.dk-avatar--xs")
    assert second_avatar.is_visible()
    assert second_avatar.locator(".dk-avatar__initials").text_content() == "E"

    # Account trigger avatar in sidebar footer
    trigger_avatar = page.locator(".dk-account-trigger .dk-avatar.dk-avatar--sm")
    assert trigger_avatar.is_visible()
    assert trigger_avatar.locator(".dk-avatar__initials").text_content() == "GH"
    assert errors == []


def test_audience_tag_filters_adopt_dk_tag_input(browser, live_server, component_data):
    user = component_data["user"]
    c1 = component_data["clients"][0]
    audience = component_data["audience"]

    url = reverse("mailing:audience_detail", args=[audience.id]) + "?section=members&include_tags=ml-zoomcamp"
    page, errors = open_page_with_client(browser, live_server, user, url, active_client_id=c1.id, viewport=DESKTOP)

    # Open advanced panel
    page.locator("details.advanced-panel summary").click()
    page.wait_for_timeout(100)

    # Fallback checkbox lists should be hidden
    assert page.locator('.tag-filter-grid [data-tag-input="include_tags"] .checkbox-list').is_hidden()
    assert page.locator('.tag-filter-grid [data-tag-input="exclude_tags"] .checkbox-list').is_hidden()

    # dakitTagInput should be mounted
    include_host = page.locator('.tag-filter-grid [data-tag-input="include_tags"]')
    include_input = include_host.locator(".dk-tag-input")
    assert include_input.is_visible()

    # Initial chosen chip for ml-zoomcamp should be rendered
    chips = include_input.locator(".dk-tag-input__chip")
    assert chips.count() == 1
    assert "ml-zoomcamp" in chips.nth(0).text_content()

    # Hidden input should be present for GET submission
    hidden_inputs = include_host.locator('input[type="hidden"][name="include_tags"]')
    assert hidden_inputs.count() == 1
    assert hidden_inputs.nth(0).get_attribute("value") == "ml-zoomcamp"

    # Type to search and add de-zoomcamp
    combobox = include_input.locator("input.dk-tag-input__input")
    combobox.click()
    combobox.fill("de")
    page.wait_for_timeout(100)

    # Option should be active in popup
    popup = include_input.locator(".dk-tag-input__popup")
    assert popup.is_visible()
    de_option = popup.locator(".dk-tag-input__option")
    assert de_option.count() == 1
    assert "de-zoomcamp" in de_option.text_content()

    # Press Enter to choose
    combobox.press("Enter")
    page.wait_for_timeout(100)

    # Now 2 chips and 2 hidden inputs
    assert chips.count() == 2
    assert hidden_inputs.count() == 2
    vals = [hidden_inputs.nth(i).get_attribute("value") for i in range(2)]
    assert "ml-zoomcamp" in vals
    assert "de-zoomcamp" in vals

    # Remove the first chip
    chips.nth(0).locator(".dk-tag-input__remove").click()
    page.wait_for_timeout(100)
    assert chips.count() == 1
    assert hidden_inputs.count() == 1
    assert hidden_inputs.nth(0).get_attribute("value") == "de-zoomcamp"

    assert errors == []


def test_contact_search_tag_filters_adopt_dk_tag_input_with_groups(browser, live_server, component_data):
    user = component_data["user"]
    c1 = component_data["clients"][0]

    url = reverse("mailing:contact_search")
    page, errors = open_page_with_client(browser, live_server, user, url, active_client_id=c1.id, viewport=DESKTOP)

    # Open advanced panel
    page.locator("details.advanced-panel summary").click()
    page.wait_for_timeout(100)

    include_host = page.locator('.tag-filter-grid [data-tag-input="include_tags"]')
    include_input = include_host.locator(".dk-tag-input")
    assert include_input.is_visible()

    combobox = include_input.locator("input.dk-tag-input__input")
    combobox.click()
    page.wait_for_timeout(100)

    # Groups should be present in the listbox popup
    popup = include_input.locator(".dk-tag-input__popup")
    assert popup.is_visible()
    group_labels = popup.locator(".dk-tag-input__group-label")
    assert group_labels.count() >= 1
    assert "students" in group_labels.first.text_content().lower()

    assert errors == []
