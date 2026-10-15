"""Browser behaviour of the dakit shell and the mailbox split in relay.

Runs the real pages against pytest-django's live server in headless
Chromium: the drawer, the account menu and client switcher keyboard
contracts (dakit docs/shell.md §11) and the list–detail phone stack
(docs/list-detail.md §6).
"""

import os

import pytest
from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY, get_user_model
from django.contrib.sessions.backends.db import SessionStore

from mailing.models import Client, InboundMessage, Organization

# Playwright's sync API runs an event loop on this thread; the ORM calls here
# are plain test setup, not request handling.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

playwright_api = pytest.importorskip("playwright.sync_api")

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as playwright:
        try:
            chromium = playwright.chromium.launch()
        except Exception as error:  # noqa: BLE001 - no browser installed locally
            pytest.skip(f"Chromium is not installed for Playwright: {error}")
        yield chromium
        chromium.close()


@pytest.fixture
def operator():
    return get_user_model().objects.create_user(
        "grace", "grace@example.com", "password", is_staff=True, first_name="Grace", last_name="Hopper"
    )


@pytest.fixture
def clients():
    organization = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    return [
        Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses"),
        Client.objects.create(organization=organization, name="Second Client", slug="second-client"),
    ]


def open_page(browser, live_server, user, path, *, viewport=DESKTOP, phone=False):
    session = SessionStore()
    session[SESSION_KEY] = str(user.pk)
    session[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    session.save()
    context = browser.new_context(viewport=viewport, has_touch=phone, is_mobile=phone)
    context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": session.session_key, "url": live_server.url}])
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(live_server.url + path)
    return page, errors


def active(page):
    return page.evaluate(
        "() => { const el = document.activeElement; return el.id || el.getAttribute('aria-label') || el.textContent.trim(); }"
    )


def test_account_menu_opens_from_the_keyboard_and_escape_returns_focus(browser, live_server, operator, clients):
    page, errors = open_page(browser, live_server, operator, "/")
    trigger = page.locator(".dk-account-trigger")
    assert trigger.get_attribute("aria-label") == "Account, Grace Hopper"

    trigger.focus()
    page.keyboard.press("Enter")
    menu = page.locator("#dk-account-menu")
    assert menu.is_visible()
    assert trigger.get_attribute("aria-expanded") == "true"
    assert active(page) == "Dark mode"
    page.keyboard.press("End")
    assert active(page) == "Sign out"
    # The menu flies out beside the sidebar, never over it.
    sidebar_right = page.locator("#dk-sidebar").bounding_box()["x"] + page.locator("#dk-sidebar").bounding_box()["width"]
    assert menu.bounding_box()["x"] >= sidebar_right

    page.keyboard.press("Escape")
    assert menu.is_hidden()
    assert active(page) == "Account, Grace Hopper"
    assert errors == []


def test_dark_mode_switch_in_the_account_menu_persists(browser, live_server, operator, clients):
    page, _ = open_page(browser, live_server, operator, "/")
    before = page.evaluate("document.documentElement.dataset.theme")
    page.locator(".dk-account-trigger").click()
    page.locator("[data-dk-theme-toggle]").click()
    after = page.evaluate("document.documentElement.dataset.theme")
    assert after != before
    page.reload()
    assert page.evaluate("document.documentElement.dataset.theme") == after


def test_sidebar_collapses_to_a_working_rail_and_remembers_it(browser, live_server, operator, clients):
    page, _ = open_page(browser, live_server, operator, "/")
    page.locator("[data-dk-sidebar-toggle]").click()
    shell = page.locator(".dk-shell")
    assert shell.get_attribute("data-sidebar") == "collapsed"
    assert page.locator("#dk-sidebar").bounding_box()["width"] == 56
    # Every destination stays reachable, labelled by a tooltip on hover.
    page.locator('.dk-nav-item[href="/inbound/"]').hover()
    tooltip = page.locator(".dk-tooltip")
    tooltip.wait_for(state="visible")
    assert tooltip.text_content() == "Inbound mail"
    page.reload()
    assert page.locator(".dk-shell").get_attribute("data-sidebar") == "collapsed"


def test_client_switcher_is_a_menu_with_typeahead(browser, live_server, operator, clients):
    page, _ = open_page(browser, live_server, operator, "/")
    brand = page.locator("button.dk-brand")
    brand.focus()
    page.keyboard.press("Enter")
    assert page.locator("#dk-brand-menu").is_visible()
    page.keyboard.press("s")
    assert active(page) == "Second Client"
    with page.expect_navigation():
        page.keyboard.press("Enter")
    assert page.locator(".dk-brand__scope").text_content() == "Second Client"


def test_phone_drawer_is_modal_and_escape_returns_to_the_menu_button(browser, live_server, operator, clients):
    page, errors = open_page(browser, live_server, operator, "/", viewport=PHONE, phone=True)
    menu_button = page.locator("[data-dk-drawer-toggle]")
    # Personal chrome moves into the bar; the drawer is navigation only.
    assert page.locator(".dk-topbar__end .dk-account-trigger").count() == 1

    menu_button.click()
    sidebar = page.locator("#dk-sidebar")
    assert sidebar.get_attribute("role") == "dialog"
    assert sidebar.get_attribute("aria-modal") == "true"
    assert page.evaluate("document.querySelector('.dk-main').inert") is True
    # Focus moves in once the drawer's slide makes it visible.
    page.wait_for_function("document.getElementById('dk-sidebar').contains(document.activeElement)")

    page.keyboard.press("Escape")
    assert page.evaluate("document.querySelector('.dk-main').inert") is False
    assert active(page) == "Open navigation"
    assert sidebar.get_attribute("role") is None
    assert errors == []


@pytest.fixture
def mailbox():
    return [
        InboundMessage.objects.create(
            message_id=f"<m{i}@example.org>",
            state="read",
            subject=f"Question {i:02d}",
            sender_address=f"person{i}@example.org",
            sender_domain="example.org",
            recipient_address="hello@relay.example",
        )
        for i in range(30)
    ]


def test_phone_mailbox_back_keeps_list_position_and_focus(browser, live_server, operator, mailbox):
    page, errors = open_page(browser, live_server, operator, "/inbound/?state=all", viewport=PHONE, phone=True)
    split = page.locator("[data-inbox-split]")
    assert split.get_attribute("data-view") == "list"
    scroller = page.locator(".dk-split__list .dk-split__scroll")
    scroller.evaluate("el => { el.scrollTop = 900; }")
    row = page.locator(".dk-split__item").nth(15)
    row.scroll_into_view_if_needed()
    position = scroller.evaluate("el => el.scrollTop")
    assert position > 0
    subject = row.locator(".dk-split__name").text_content()

    row.click()
    page.wait_for_function("document.querySelector('[data-inbox-split]').dataset.view === 'detail'")
    assert "message=" in page.url
    assert page.locator("#message-title").text_content() == subject
    assert active(page) == "message-title"
    assert page.locator(".dk-split__list").is_hidden()

    page.locator(".dk-split__back").click()
    assert split.get_attribute("data-view") == "list"
    assert "message=" not in page.url
    assert scroller.evaluate("el => el.scrollTop") == position
    assert page.evaluate("document.activeElement.querySelector('.dk-split__name').textContent") == subject
    assert errors == []


def test_desktop_mailbox_pick_swaps_the_detail_and_back_restores_it(browser, live_server, operator, mailbox):
    page, errors = open_page(browser, live_server, operator, "/inbound/?state=all")
    assert "Select a message" in page.locator("[data-inbox-detail]").text_content()

    first = page.locator(".dk-split__item").nth(0)
    second = page.locator(".dk-split__item").nth(1)
    first.click()
    page.wait_for_selector("#message-title")
    first_subject = page.locator("#message-title").text_content()
    second.click()
    page.wait_for_function("t => document.querySelector('#message-title').textContent !== t", arg=first_subject)
    assert page.locator('.dk-split__item[aria-current="true"]').count() == 1
    # Focus stays in the list on desktop, so arrows keep working.
    assert page.evaluate("!!document.activeElement.closest('.dk-split__list')")
    page.keyboard.press("ArrowUp")
    assert page.evaluate("document.activeElement.querySelector('.dk-split__name').textContent") == first_subject

    page.go_back()
    page.wait_for_function("t => document.querySelector('#message-title').textContent === t", arg=first_subject)
    assert page.locator('.dk-split__item[aria-current="true"] .dk-split__name').text_content() == first_subject
    assert errors == []


def test_desktop_history_back_to_no_selection_empties_the_detail(browser, live_server, operator, mailbox):
    page, errors = open_page(browser, live_server, operator, "/inbound/?state=all")
    page.locator(".dk-split__item").nth(3).click()
    page.wait_for_selector("#message-title")

    page.go_back()
    page.wait_for_function("!location.search.includes('message=')")
    page.wait_for_selector("[data-inbox-detail] .dk-empty")
    assert page.locator('.dk-split__item[aria-current="true"]').count() == 0
    assert page.locator("#message-title").count() == 0
    assert "Mark unread" not in page.locator("[data-inbox-detail]").text_content()
    assert page.title() == "Inbox | Relay"
    assert errors == []


def delay_message_fetches(page, ms):
    page.evaluate(
        """ms => {
          const original = window.fetch;
          window.fetch = (...args) => new Promise((resolve, reject) =>
            setTimeout(() => original(...args).then(resolve, reject), ms));
        }""",
        ms,
    )


def test_phone_back_before_a_slow_message_arrives_keeps_the_list(browser, live_server, operator, mailbox):
    for theme in ("light", "dark"):
        page, errors = open_page(browser, live_server, operator, "/inbound/?state=all", viewport=PHONE, phone=True)
        page.evaluate("t => { document.documentElement.dataset.theme = t; }", theme)
        delay_message_fetches(page, 700)
        row = page.locator(".dk-split__item").nth(5)
        subject = row.locator(".dk-split__name").text_content()
        row.click()
        page.go_back()
        page.wait_for_timeout(1200)  # well past the delayed response

        split = page.locator("[data-inbox-split]")
        assert "message=" not in page.url
        assert split.get_attribute("data-view") == "list"
        assert page.locator("#message-title").count() == 0
        assert page.locator("[data-inbox-detail] .dk-empty").count() == 1
        assert page.evaluate("document.activeElement.querySelector('.dk-split__name')?.textContent") == subject
        assert errors == []
        page.context.close()


def test_phone_browser_back_and_forward_move_focus_with_the_pane(browser, live_server, operator, mailbox):
    page, errors = open_page(browser, live_server, operator, "/inbound/?state=all", viewport=PHONE, phone=True)
    scroller = page.locator(".dk-split__list .dk-split__scroll")
    row = page.locator(".dk-split__item").nth(20)
    row.scroll_into_view_if_needed()
    position = scroller.evaluate("el => el.scrollTop")
    assert position > 0
    subject = row.locator(".dk-split__name").text_content()
    row.click()
    page.wait_for_selector("#message-title")

    page.go_back()
    page.wait_for_function("document.querySelector('[data-inbox-split]').dataset.view === 'list'")
    assert scroller.evaluate("el => el.scrollTop") == position
    assert page.evaluate("document.activeElement.querySelector('.dk-split__name')?.textContent") == subject
    focused = page.evaluate("document.activeElement.getBoundingClientRect().toJSON()")
    box = scroller.bounding_box()
    assert box["y"] <= focused["y"] and focused["bottom"] <= box["y"] + box["height"]

    page.go_forward()
    page.wait_for_function("document.querySelector('[data-inbox-split]').dataset.view === 'detail'")
    page.wait_for_function("document.activeElement && document.activeElement.id === 'message-title'")
    assert page.locator("#message-title").text_content() == subject
    assert errors == []
