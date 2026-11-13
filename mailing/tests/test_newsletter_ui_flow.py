"""Operator UI for newsletters: campaign chooser, new newsletter from a template, the block
editor (checklist, inline block forms, block actions), assembling the campaign draft and the
notices on the campaign pages. Network sources are patched; nothing reaches the internet."""

from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import Client as HttpClient
from django.urls import reverse

from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import Audience, Campaign, CampaignStatus, Client, NewsletterBlock, NewsletterIssue, Organization
from mailing.services import newsletter_sources as sources
from mailing.services import newsletters as svc

pytestmark = pytest.mark.django_db

EVENTS = {"kicker": "EVENTS", "title": "Upcoming Events",
          "events": "Mon, November 16 · 5:00 PM CET · Webinar | Course launch | https://luma.com/abc | with Alexey",
          "footer": "[See all upcoming events](https://datatalks.club/events.html)"}
RECORDING = {"title": "Workshop Recording: Build an agent", "meta": "Workshop · Nov 10",
             "image_url": "https://img.youtube.com/vi/abc/hqdefault.jpg", "image_link": "https://youtu.be/abc",
             "cta_text": "Watch the recording", "cta_url": "https://youtu.be/abc"}
BOOK = {"title": "Book of the week: Designing ML Systems", "body": "Ask the author anything.",
        "cta_text": "Ask a question", "cta_url": "https://datatalks.club/books/ml.html",
        "image_url": "https://datatalks.club/images/books/ml.jpg"}


@pytest.fixture
def org():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def client_record(org):
    return Client.objects.create(organization=org, name="DTC newsletter", slug="dtc-newsletter")


@pytest.fixture
def audience(org):
    return Audience.objects.create(organization=org, name="DTC subscribers", slug="dtc")


@pytest.fixture
def other_client(org):
    return Client.objects.create(organization=org, name="Other", slug="other")


def login(client_record):
    user = get_user_model().objects.create_user("valeriia", "valeriia@datatalks.club", is_staff=True)
    http = HttpClient()
    http.force_login(user)
    session = http.session
    session[ACTIVE_CLIENT_SESSION_KEY] = client_record.id
    session.save()
    return http


@pytest.fixture
def ui(client_record):
    return login(client_record)


@pytest.fixture
def autofill(monkeypatch):
    monkeypatch.setattr(sources, "autofill_enabled", lambda: True)
    monkeypatch.setattr(sources, "events_list_data", lambda day: dict(EVENTS))
    monkeypatch.setattr(sources, "recordings_data", lambda day: [dict(RECORDING)])
    monkeypatch.setattr(sources, "podcast_data", lambda day: [])
    monkeypatch.setattr(sources, "alexey_article_data", lambda day: None)
    monkeypatch.setattr(sources, "book_data", lambda day: dict(BOOK))


@pytest.fixture
def issue(client_record):
    issue = svc.create_issue(client_record, {"key": "weekly-303", "subject": "DataTalks.Club Weekly #303",
                                             "issue_number": "303", "publication_date": "2026-11-16"})
    svc.add_block(issue, "intro", {"issue_label": "#303", "summary": "This week: **news**."})
    svc.add_block(issue, "sponsor", {}, needs_review=True, source="placeholder", source_note="Paste the sponsor copy.")
    svc.add_block(issue, "book_of_week", BOOK, source="website:books", source_note="Filled from the books page.")
    return issue


def blocks(issue):
    return list(issue.blocks.order_by("position", "id"))


def post(ui, client_record, url, data=None, **kwargs):
    return ui.post(url, {"operator_client_id": client_record.id, **(data or {})}, **kwargs)


def make_ready(issue):
    sponsor = issue.blocks.get(block_type="sponsor")
    svc.update_block(sponsor, {"title": "Try Acme", "cta_text": "Try it", "cta_url": "https://acme.dev"})


# --------------------------------------------------------------------------- campaigns: chooser

def test_new_campaign_is_a_chooser_between_newsletter_and_regular(ui, client_record):
    response = ui.get(reverse("mailing:campaign_create"))
    html = response.content.decode()
    assert response.status_code == 200
    assert "What do you want to create?" in html
    assert f'href="{reverse("mailing:newsletter_new")}"' in html
    assert f'href="{reverse("mailing:campaign_create")}?type=regular"' in html
    assert 'name="html_body"' not in html


def test_regular_campaign_form_still_creates_campaigns(ui, client_record, audience):
    form = ui.get(reverse("mailing:campaign_create") + "?type=regular")
    assert form.status_code == 200 and b'name="html_body"' in form.content
    response = post(ui, client_record, reverse("mailing:campaign_create") + "?type=regular", {
        "client": client_record.id, "audience": audience.id, "subject": "One-off", "html_body": "<p>Hi</p>",
        "text_body": "Hi", "send_mode": "now", "schedule_timezone": "UTC"})
    campaign = Campaign.objects.get(subject="One-off")
    assert response.status_code == 302 and response["Location"] == reverse("mailing:campaign_detail",
                                                                           args=[campaign.id])
    assert campaign.status == CampaignStatus.DRAFT


# --------------------------------------------------------------------------- new newsletter

def test_new_newsletter_page_is_prefilled_from_the_template(ui, client_record):
    response = ui.get(reverse("mailing:newsletter_new"))
    html = response.content.decode()
    defaults = svc.issue_defaults(client_record, "dtc-weekly")
    assert response.status_code == 200
    assert 'value="dtc-weekly" checked' in html
    assert f'value="{defaults["subject"]}"' in html
    assert f'value="{defaults["issue_number"]}"' in html
    assert f'value="{defaults["publication_date"]}"' in html
    assert f'value="{defaults["key"]}"' in html
    assert "also used as the campaign key" in html
    assert "Creating… fetching events, recordings and the book of the week" in html
    blank = ui.get(reverse("mailing:newsletter_new") + "?template=blank").content.decode()
    assert 'value="blank" checked' in blank and f'value="{defaults["subject"]}"' not in blank


def test_create_newsletter_from_template_fills_blocks_and_reports(ui, client_record, autofill):
    response = post(ui, client_record, reverse("mailing:newsletter_new"), {
        "template": "dtc-weekly", "key": "weekly-303", "subject": "DataTalks.Club Weekly #303",
        "issue_number": "303", "publication_date": "2026-11-16", "preview_text": ""}, follow=True)
    issue = NewsletterIssue.objects.get(client=client_record, key="weekly-303")
    assert response.redirect_chain[-1][0] == reverse("mailing:newsletter_detail", args=[issue.pk])
    types = [block.block_type for block in blocks(issue)]
    assert types == ["intro", "sponsor", "course_update", "events_list", "recording", "book_of_week"]
    html = response.content.decode()
    attention = sum(1 for item in svc.issue_problems(issue) if item["block_id"])
    assert f"Newsletter created with 6 blocks (3 filled automatically). {attention} blocks need your content" in html
    assert "Not added:" in html and "Alexey" in html  # skipped article / podcast are explained
    assert issue.template_key == "dtc-weekly"


def test_create_newsletter_validation_keeps_values(ui, client_record):
    svc.create_issue(client_record, {"key": "weekly-303", "subject": "Existing"})
    response = post(ui, client_record, reverse("mailing:newsletter_new"), {
        "template": "dtc-weekly", "key": "weekly-303", "subject": "My subject", "issue_number": "abc",
        "publication_date": "2026-11-16"})
    html = response.content.decode()
    assert response.status_code == 200
    assert "The newsletter was not created." in html
    assert "Enter a whole number, e.g. 303." in html
    assert 'value="My subject"' in html and 'value="abc"' in html
    assert NewsletterIssue.objects.filter(client=client_record).count() == 1
    duplicate = post(ui, client_record, reverse("mailing:newsletter_new"), {
        "template": "blank", "key": "weekly-303", "subject": "Again"}).content.decode()
    assert "Another newsletter already uses this key." in duplicate
    assert "Open the existing newsletter" in duplicate


# --------------------------------------------------------------------------- editor

def test_editor_shows_checklist_badges_and_live_preview(ui, issue):
    html = ui.get(reverse("mailing:newsletter_detail", args=[issue.pk])).content.decode()
    intro, sponsor, book = blocks(issue)
    assert "1 block still needs your attention" in html
    assert f'href="#block-{sponsor.pk}"' in html and "Title is required" in html
    assert "Needs content" in html and "Auto-filled" in html and "Ready" in html
    assert "No sponsor this week? Turn this block into" in html
    assert "Refresh from source" in html  # the book block came from the website
    # Needs-content blocks are open for editing; ready blocks show a rendered preview.
    assert html.count("data-nl-edit open") == 1
    assert 'srcdoc="&lt;!doctype html&gt;' in html and 'sandbox=""' in html
    assert "Needs content:</strong>" not in html  # email HTML is only inside escaped srcdoc
    assert "Needs content:&lt;/strong&gt;" in html  # the live preview marks unfinished blocks
    assert "Create campaign draft" in html and "disabled" in html
    assert "newsletter-editor.js" in html


def test_editor_all_ready_state(ui, issue, audience):
    make_ready(issue)
    html = ui.get(reverse("mailing:newsletter_detail", args=[issue.pk])).content.decode()
    assert "All blocks are ready" in html
    assert "data-nl-edit open" not in html
    assert f'<option value="{audience.id}" selected>' in html
    assert "Nothing is sent. You review, test and schedule it from the campaign page." in html


def test_inline_block_save_success_and_validation_error(ui, client_record, issue):
    sponsor = blocks(issue)[1]
    url = reverse("mailing:newsletter_block_edit", args=[issue.pk, sponsor.pk])
    bad = post(ui, client_record, url, {"title": "Try Acme", "body": "Fast **pipelines**", "cta_url": "ftp://x"})
    html = bad.content.decode()
    assert bad.status_code == 200
    assert "Block not saved" in html
    assert 'value="Try Acme"' in html and "Fast **pipelines**" in html
    assert "Enter a full link starting with https://" in html and "Fill this in." in html
    assert 'aria-invalid="true"' in html
    sponsor.refresh_from_db()
    assert sponsor.needs_review and "title" not in sponsor.data

    good = post(ui, client_record, url, {"title": "Try Acme", "cta_text": "Try it", "cta_url": "https://acme.dev"})
    assert good.status_code == 302
    assert good["Location"] == reverse("mailing:newsletter_detail", args=[issue.pk]) + f"#block-{sponsor.pk}"
    sponsor.refresh_from_db()
    assert sponsor.data["title"] == "Try Acme" and not sponsor.needs_review
    tested = post(ui, client_record, url, {"title": "Try Acme", "cta_text": "Try it", "cta_url": "https://acme.dev",
                                           "then": "test"})
    assert tested["Location"].endswith(f"?test_block={sponsor.pk}#newsletter-test-send")


def test_old_block_urls_render_the_editor(ui, issue):
    block = blocks(issue)[0]
    edit = ui.get(reverse("mailing:newsletter_block_edit", args=[issue.pk, block.pk]))
    assert edit.status_code == 200 and b"data-nl-editor" in edit.content
    new = ui.get(reverse("mailing:newsletter_block_new", args=[issue.pk]) + "?type=feature")
    assert new.status_code == 200 and b"New block: Feature" in new.content
    assert ui.get(reverse("mailing:newsletter_block_new", args=[issue.pk]) + "?type=nope").status_code == 302


def test_change_type_turns_sponsor_into_course_promo(ui, client_record, issue):
    sponsor = blocks(issue)[1]
    response = post(ui, client_record, reverse("mailing:newsletter_block_action", args=[issue.pk, sponsor.pk]),
                    {"action": "change_type", "new_type": "course_promo"}, follow=True)
    sponsor.refresh_from_db()
    assert sponsor.block_type == "course_promo" and sponsor.needs_review
    assert "SPONSORED" not in sponsor.data.values()
    assert "is now “Course promo (when no sponsor)”" in response.content.decode()


def test_duplicate_move_and_delete_with_confirmation(ui, client_record, issue):
    intro, sponsor, book = blocks(issue)
    action = lambda block: reverse("mailing:newsletter_block_action", args=[issue.pk, block.pk])  # noqa: E731
    response = post(ui, client_record, action(book), {"action": "duplicate"})
    copy = blocks(issue)[3]
    assert copy.block_type == "book_of_week" and copy.pk != book.pk
    assert response["Location"].endswith(f"#block-{copy.pk}")

    post(ui, client_record, action(book), {"action": "up"})
    assert [block.pk for block in blocks(issue)] == [intro.pk, book.pk, sponsor.pk, copy.pk]
    post(ui, client_record, action(intro), {"action": "down"})
    assert [block.pk for block in blocks(issue)] == [book.pk, intro.pk, sponsor.pk, copy.pk]

    unconfirmed = post(ui, client_record, action(copy), {"action": "delete"}, follow=True)
    assert NewsletterBlock.objects.filter(pk=copy.pk).exists()
    assert "Tick the confirmation box" in unconfirmed.content.decode()
    post(ui, client_record, action(copy), {"action": "delete", "confirm_delete": "1"})
    assert not NewsletterBlock.objects.filter(pk=copy.pk).exists()


def test_refresh_from_source_success_and_unavailable(ui, client_record, issue, monkeypatch):
    book = blocks(issue)[2]
    url = reverse("mailing:newsletter_block_action", args=[issue.pk, book.pk])
    monkeypatch.setattr(sources, "autofill_enabled", lambda: True)
    monkeypatch.setattr(sources, "book_data", lambda day: {**BOOK, "title": "Book of the week: Fresh title"})
    ok = post(ui, client_record, url, {"action": "refill"}, follow=True)
    book.refresh_from_db()
    assert book.data["title"] == "Book of the week: Fresh title"
    assert "refreshed from its source" in ok.content.decode()

    monkeypatch.setattr(sources, "book_data", lambda day: None)
    failed = post(ui, client_record, url, {"action": "refill"}, follow=True)
    book.refresh_from_db()
    assert book.data["title"] == "Book of the week: Fresh title"
    assert "Could not load fresh content right now" in failed.content.decode()


def test_add_block_inline_at_a_position(ui, client_record, issue):
    page = ui.get(reverse("mailing:newsletter_detail", args=[issue.pk]) + "?add=feature&at=1").content.decode()
    assert "New block: Feature" in page and 'name="position" value="1"' in page
    response = post(ui, client_record, reverse("mailing:newsletter_block_new", args=[issue.pk]),
                    {"block_type": "feature", "position": "1", "title": "Open-source spotlight"})
    added = blocks(issue)[1]
    assert added.block_type == "feature" and added.data["title"] == "Open-source spotlight"
    assert response["Location"].endswith(f"#block-{added.pk}")
    invalid = post(ui, client_record, reverse("mailing:newsletter_block_new", args=[issue.pk]),
                   {"block_type": "feature", "position": "0", "body": "No title yet"})
    assert invalid.status_code == 200 and "New block: Feature" in invalid.content.decode()
    assert "No title yet" in invalid.content.decode() and issue.blocks.count() == 4


def test_issue_details_save_and_error(ui, client_record, issue):
    url = reverse("mailing:newsletter_detail", args=[issue.pk])
    response = post(ui, client_record, url, {"subject": "New subject", "preview_text": "Inbox line",
                                             "issue_number": "304", "publication_date": "2026-11-23"})
    assert response["Location"].endswith("#newsletter-details")
    issue.refresh_from_db()
    assert (issue.subject, issue.preview_text, issue.issue_number) == ("New subject", "Inbox line", 304)
    bad = post(ui, client_record, url, {"subject": "", "preview_text": "Typed", "issue_number": "x",
                                        "publication_date": ""})
    html = bad.content.decode()
    assert bad.status_code == 200 and "Enter a subject." in html and 'value="Typed"' in html
    issue.refresh_from_db()
    assert issue.subject == "New subject"


# --------------------------------------------------------------------------- campaign draft

def test_assemble_is_refused_while_blocks_need_attention(ui, client_record, issue, audience):
    response = post(ui, client_record, reverse("mailing:newsletter_assemble", args=[issue.pk]),
                    {"audience": audience.id}, follow=True)
    assert not Campaign.objects.exists()
    assert "Some blocks still need your attention" in response.content.decode()


def test_assemble_creates_then_updates_a_draft(ui, client_record, issue, audience):
    make_ready(issue)
    url = reverse("mailing:newsletter_assemble", args=[issue.pk])
    response = post(ui, client_record, url, {"audience": audience.id}, follow=True)
    campaign = Campaign.objects.get()
    assert campaign.status == CampaignStatus.DRAFT and campaign.audience == audience
    assert response.redirect_chain[-1][0] == reverse("mailing:campaign_detail", args=[campaign.pk])
    html = response.content.decode()
    assert "Campaign draft created from the newsletter. Nothing has been sent." in html
    assert "This campaign was built from newsletter blocks." in html
    assert reverse("mailing:newsletter_detail", args=[issue.pk]) in html

    editor = ui.get(reverse("mailing:newsletter_detail", args=[issue.pk])).content.decode()
    assert "Update campaign draft" in editor and "Open campaign" in editor
    issue.refresh_from_db()  # update_issue saves every field, so it must not see a stale campaign link
    svc.update_issue(issue, {"subject": "Weekly #303, updated"})
    again = post(ui, client_record, url, {"audience": audience.id}, follow=True)
    campaign.refresh_from_db()
    assert Campaign.objects.count() == 1 and campaign.subject == "Weekly #303, updated"
    assert "Campaign draft updated with the latest blocks" in again.content.decode()


def test_assemble_refuses_a_campaign_that_is_no_longer_a_draft(ui, client_record, issue, audience):
    make_ready(issue)
    campaign, _ = svc.assemble_campaign(issue, audience=audience)
    Campaign.objects.filter(pk=campaign.pk).update(status=CampaignStatus.SENT)
    response = post(ui, client_record, reverse("mailing:newsletter_assemble", args=[issue.pk]),
                    {"audience": audience.id}, follow=True)
    html = response.content.decode()
    assert "already queued, scheduled or sent, so the blocks can no longer change it" in html
    assert "The campaign was already queued or sent." in html  # the editor panel explains it too


def test_assemble_reports_a_key_in_use_and_a_missing_audience(ui, client_record, issue, audience):
    make_ready(issue)
    url = reverse("mailing:newsletter_assemble", args=[issue.pk])
    missing = post(ui, client_record, url, {"audience": ""}, follow=True)
    assert "Choose who receives the newsletter" in missing.content.decode()
    Campaign.objects.create(client=client_record, audience=audience, external_key="weekly-303", subject="Other")
    taken = post(ui, client_record, url, {"audience": audience.id}, follow=True)
    assert "already uses the key “weekly-303”" in taken.content.decode()


def test_campaign_edit_warns_that_hand_edits_are_overwritten(ui, client_record, issue, audience):
    make_ready(issue)
    campaign, _ = svc.assemble_campaign(issue, audience=audience)
    html = ui.get(reverse("mailing:campaign_edit", args=[campaign.pk])).content.decode()
    assert "This campaign was built from newsletter blocks." in html
    assert "overwritten the next time the draft is updated from the blocks" in html
    plain = Campaign.objects.create(client=client_record, audience=audience, subject="Plain")
    assert "built from newsletter blocks" not in ui.get(
        reverse("mailing:campaign_detail", args=[plain.pk])).content.decode()


# --------------------------------------------------------------------------- list, nav, access

def test_newsletter_list_shows_status_and_new_button(ui, client_record, issue, audience):
    html = ui.get(reverse("mailing:newsletter_list")).content.decode()
    assert f'href="{reverse("mailing:newsletter_new")}"' in html and "New newsletter" in html
    assert "1 needs attention" in html and "Not created yet" in html
    assert '<span class="dk-nav-item__label">Newsletters</span>' in html
    make_ready(issue)
    campaign, _ = svc.assemble_campaign(issue, audience=audience)
    html = ui.get(reverse("mailing:newsletter_list")).content.decode()
    assert "All blocks ready" in html and reverse("mailing:campaign_detail", args=[campaign.pk]) in html


def test_other_clients_issues_and_blocks_are_404(client_record, other_client, issue):
    ui = login(other_client)
    block = blocks(issue)[0]
    data = {"operator_client_id": other_client.id}
    assert ui.get(reverse("mailing:newsletter_detail", args=[issue.pk])).status_code == 404
    assert ui.get(reverse("mailing:newsletter_block_edit", args=[issue.pk, block.pk])).status_code == 404
    assert ui.post(reverse("mailing:newsletter_block_action", args=[issue.pk, block.pk]),
                   {**data, "action": "delete", "confirm_delete": "1"}).status_code == 404
    assert ui.post(reverse("mailing:newsletter_assemble", args=[issue.pk]), data).status_code == 404
    assert NewsletterBlock.objects.filter(pk=block.pk).exists()


def test_non_staff_is_blocked(client_record, issue):
    user = get_user_model().objects.create_user("guest", "guest@example.com")
    http = HttpClient()
    http.force_login(user)
    for url in (reverse("mailing:newsletter_new"), reverse("mailing:newsletter_detail", args=[issue.pk])):
        response = http.get(url)
        assert response.status_code == 302 and "/login/" in response["Location"]
    assert http.post(reverse("mailing:newsletter_assemble", args=[issue.pk])).status_code == 302
    assert not Campaign.objects.exists()


def test_editor_text_areas_wrap_long_text():
    # The shared control rule sets white-space: nowrap; paragraphs typed into a
    # block must wrap instead of scrolling sideways on one line.
    css = Path(finders.find("mailing/css/app.css")).read_text()
    rule = css.split("[data-nl-editor] textarea {", 1)[1].split("}", 1)[0]
    assert "white-space: pre-wrap" in rule
