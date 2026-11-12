"""Newsletter templates, auto-fill from public sources, and assembly into a draft campaign."""

import datetime as dt
import json

import pytest
from django.core.cache import cache
from django.test import Client as HttpClient
from django_tasks_db.models import DBTaskResult

from mailing.models import (
    Audience,
    Campaign,
    CampaignDispatch,
    CampaignRecipient,
    CampaignStatus,
    Client,
    NewsletterBlock,
    NewsletterIssue,
    Organization,
)
from mailing.services import campaigns as campaign_services
from mailing.services import newsletter_sources as sources
from mailing.services import newsletters as svc
from mailing.services.api_errors import ApiValidationError
from mailing.services.auth import create_client_api_key

pytestmark = pytest.mark.django_db

RAW = sources.DEFAULT_WEBSITE_RAW
API = sources.DEFAULT_WEBSITE_API
MONDAY = dt.date(2026, 11, 16)

EVENTS_YAML = """\
# Upcoming events (newest first, not strictly ordered)
- time: 2026-11-16 17:00:00
  title: "Production AI Agents for Generative Media: Course Launch"
  speakers: [alexeygrigorev]
  type: webinar
  link: https://luma.com/xey7h50c


- time: 2026-12-15 15:00
  title: "Shipping Reliable LLMs and the Rise of AI Engineering"
  speakers: [louisfrancoisbouchard]
  type: podcast
  link: https://luma.com/nassme8x

- time: "2026-11-24 10:00"
  title: "Data Engineering Zoomcamp | Office Hours"
  speakers: [alexeygrigorev, mikhailrozhkov, ghostspeaker]
  type: workshop
  link: https://luma.com/office

- time: 2026-11-10 17:00:00
  title: "Production AI Agents for Generative Media: Pre-Course Live Q&A"
  speakers: [alexeygrigorev, mikhailrozhkov]
  type: webinar
  link: https://luma.com/i4x6x2zy
  youtube: https://www.youtube.com/watch?v=iACtrkiXndw

- time: 2026-11-03 16:30:00
  title: "Build a Media Agent: CPU Agent, GPU Models as Tools"
  speakers: [alexeygrigorev]
  type: workshop
  link: https://luma.com/tj2nbrl2
  youtube: https://www.youtube.com/watch?v=XzS6MrNfrcA

- time: 2026-11-12 17:00:00
  title: "Navigating the Maze of Production Machine Learning"
  speakers: [migueloteropedrido]
  type: podcast
  link: https://luma.com/twzo5ib0
  youtube: https://youtu.be/xC5lqvfSfLU

- time: 2026-11-13 17:00
  title: "Podcast still being edited"
  speakers: [migueloteropedrido]
  type: podcast
  link: https://luma.com/edit
  extra_key: kept by old entries
"""

PEOPLE = {
    "alexeygrigorev": 'Alexey Grigorev',
    "mikhailrozhkov": 'Mikhail Rozhkov',
    "migueloteropedrido": 'Miguel Otero Pedrido',
    "chiphuyen": 'Chip Huyen',
}

BOOKS_LISTING = json.dumps([
    {"name": "20261012-designing-ai-systems.md", "type": "file"},
    {"name": "20261116-ai-engineering.md", "type": "file"},
    {"name": "_template.md", "type": "file"},
])

BOOK_AI_ENGINEERING = """\
---
authors:
- chiphuyen
cover: images/books/20261116-ai-engineering/cover.jpg
start: 2026-11-16 00:00:00
end: 2026-11-20 23:59:59
links:
- link: https://www.oreilly.com/library/view/ai-engineering/9781098166298/
  text: Book's page
title: AI Engineering
---

Recent breakthroughs in AI have not only increased demand for AI products,
they've also lowered the barriers to entry. This book covers the process of
building applications with readily available foundation models.

Second paragraph that is not used.
"""

BOOK_DESIGNING = """\
---
authors: [suhassuresha]
cover: images/books/20261012-designing-ai-systems/cover.jpg
start: 2026-10-12 00:00:00
end: 2026-10-16 23:59:59
title: Designing AI Systems
---

AI applications need much more than a connection to a model.
"""

FEED = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0"><channel>
<title>Alexey On Data</title>
<item>
  <title><![CDATA[Dark Software Factories]]></title>
  <description><![CDATA[Why I <b>stopped</b> reading code from agents &amp; what replaced it]]></description>
  <link>https://aishippingblog.com/p/dark-software-factories</link>
  <pubDate>Fri, 13 Nov 2026 09:43:50 GMT</pubDate>
  <enclosure url="https://substackcdn.com/image/fetch/dark.png" length="0" type="image/jpeg"/>
</item>
<item>
  <title>Do We Still Need Kubernetes?</title>
  <description>Agents in production</description>
  <link>https://aishippingblog.com/p/kubernetes</link>
  <pubDate>Fri, 16 Oct 2026 08:00:00 GMT</pubDate>
</item>
</channel></rss>
"""


def _payloads():
    pages = {
        f"{RAW}/_data/events.yaml": EVENTS_YAML,
        f"{API}/_books": BOOKS_LISTING,
        f"{RAW}/_books/20261116-ai-engineering.md": BOOK_AI_ENGINEERING,
        f"{RAW}/_books/20261012-designing-ai-systems.md": BOOK_DESIGNING,
        sources.DEFAULT_SUBSTACK_FEED: FEED,
    }
    for slug, name in PEOPLE.items():
        pages[f"{RAW}/_people/{slug}.md"] = f'---\nshort: {slug}\ntitle: "{name}"\n---\n\nBio.\n'
    return {url: body.encode() for url, body in pages.items()}


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def web(monkeypatch):
    """Serve fixture payloads through the single network entry point; record requested URLs."""
    pages = _payloads()
    requested = []

    def fake_get(url):
        requested.append(url)
        return pages.get(url)

    monkeypatch.setattr(sources, "_http_get", fake_get)
    return requested


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(sources, "_http_get", lambda url: None)


@pytest.fixture
def client_record():
    org = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    Audience.objects.create(organization=org, name="DataTalks.Club", slug="datatalks-club")
    return Client.objects.create(
        organization=org, name="DTC newsletter", slug="dtc-newsletter", default_sender_id="alexey",
        sender_emails=[{"id": "alexey", "email": "Alexey Grigorev <alexey@datatalks.club>"}],
    )


@pytest.fixture
def audience(client_record):
    return Audience.objects.get(slug="datatalks-club")


@pytest.fixture
def foreign_audience():
    org = Organization.objects.create(name="AI Shipping Labs", slug="aishl")
    return Audience.objects.create(organization=org, name="AISHL", slug="aishl-members")


@pytest.fixture
def ready_issue(client_record):
    issue = svc.create_issue(client_record, {"key": "weekly-303", "subject": "DataTalks.Club Weekly #303",
                                             "preview_text": "Course launch", "issue_number": 303,
                                             "publication_date": "2026-11-16"})
    svc.add_block(issue, "intro", {"issue_label": "#303 · November 16, 2026", "summary": "This week: **launch**."})
    svc.add_block(issue, "sponsor", {"title": "Try Acme", "cta_text": "Try it", "cta_url": "https://acme.dev/"})
    return issue


# --------------------------------------------------------------------------- sources

def test_events_list_formats_berlin_time_type_and_speaker_names(web):
    data = sources.events_list_data(MONDAY)
    assert data["events"].splitlines() == [
        "Mon, November 16 · 5:00 PM CET · Webinar | Production AI Agents for Generative Media: Course Launch"
        " | https://luma.com/xey7h50c | with Alexey Grigorev",
        "Tue, November 24 · 10:00 AM CET · Workshop | Data Engineering Zoomcamp / Office Hours"
        " | https://luma.com/office | with Alexey Grigorev, Mikhail Rozhkov, and ghostspeaker",
    ]
    assert "lu.ma/dtc-events" in data["footer"] and "calendar.google.com" in data["footer"]
    # The block validates strictly, so it can go out as is.
    svc.validate_block("events_list", data)


def test_event_time_label_switches_to_cest_in_summer():
    summer = sources._parse_event_time("2026-06-02 17:00")
    assert sources.event_when(summer) == "Tue, June 2 · 5:00 PM CEST"
    assert sources.event_when(sources._parse_event_time(dt.datetime(2026, 11, 16, 9, 5))) == \
        "Mon, November 16 · 9:05 AM CET"


def test_recordings_and_podcasts_only_take_recorded_events_from_the_past_week(web):
    recordings = sources.recordings_data(MONDAY)
    assert [item["title"] for item in recordings] == [
        "Webinar Recording: Production AI Agents for Generative Media: Pre-Course Live Q&A"]
    assert recordings[0] == {
        "title": "Webinar Recording: Production AI Agents for Generative Media: Pre-Course Live Q&A",
        "meta": "Webinar · Nov 10 · with Alexey Grigorev and Mikhail Rozhkov",
        "image_url": "https://img.youtube.com/vi/iACtrkiXndw/hqdefault.jpg",
        "image_link": "https://www.youtube.com/watch?v=iACtrkiXndw",
        "cta_text": "Watch the recording",
        "cta_url": "https://www.youtube.com/watch?v=iACtrkiXndw",
    }
    podcasts = sources.podcast_data(MONDAY)
    assert [(item["title"], item["image_url"], item["cta_text"]) for item in podcasts] == [
        ("Podcast Recording: Navigating the Maze of Production Machine Learning",
         "https://img.youtube.com/vi/xC5lqvfSfLU/hqdefault.jpg", "Listen to the episode")]
    # A week later the Nov 10 recording is out of the window and the Nov 3 one never was in.
    assert sources.recordings_data(MONDAY + dt.timedelta(days=7)) == []
    assert sources.recordings_data(MONDAY, days=14)[0]["title"].startswith("Workshop Recording: Build a Media Agent")


def test_book_of_the_week_overlaps_the_issue_week(web):
    book = sources.book_data(MONDAY)
    assert book["title"] == "Book of the Week: AI Engineering"
    assert book["meta"] == "by Chip Huyen"
    assert book["image_url"] == "https://datatalks.club/images/books/20261116-ai-engineering/cover.jpg"
    assert book["cta_url"] == book["image_link"] == "https://datatalks.club/books/20261116-ai-engineering.html"
    assert book["body"].startswith("Recent breakthroughs in AI") and "Second paragraph" not in book["body"]
    assert sources.book_data(dt.date(2026, 10, 12))["title"] == "Book of the Week: Designing AI Systems"
    assert sources.book_data(dt.date(2026, 11, 30)) is None


def test_long_book_description_is_cut_at_a_sentence():
    text = "First sentence is here. " * 40
    trimmed = sources._trim_sentences(text, 600)
    assert len(trimmed) <= 600 and trimmed.endswith("here.")


def test_substack_article_is_the_latest_post_before_publication(web):
    article = sources.alexey_article_data(MONDAY)
    assert article == {
        "title": "From Alexey on Data: Dark Software Factories",
        "image_url": "https://substackcdn.com/image/fetch/dark.png",
        "image_link": "https://aishippingblog.com/p/dark-software-factories",
        "body": "Why I stopped reading code from agents & what replaced it",
        "cta_text": "Read the article",
        "cta_url": "https://aishippingblog.com/p/dark-software-factories",
    }
    assert sources.alexey_article_data(dt.date(2026, 12, 7)) is None


@pytest.mark.parametrize("body", [None, b"", b"\xff\xfe not utf8", b"- time: [unclosed\n  : :", b"<rss><item>",
                                  b'{"not": "a list"}', b"just text"])
def test_sources_return_nothing_on_failure_or_garbage(monkeypatch, body):
    monkeypatch.setattr(sources, "_http_get", lambda url: body)
    assert sources.events_list_data(MONDAY) is None
    assert sources.recordings_data(MONDAY) == []
    assert sources.podcast_data(MONDAY) == []
    assert sources.book_data(MONDAY) is None
    assert sources.alexey_article_data(MONDAY) is None


def test_sources_never_raise_when_the_fetcher_raises(monkeypatch):
    def boom(url):
        raise TimeoutError("slow")

    monkeypatch.setattr(sources, "_http_get", boom)
    assert sources.events_list_data(MONDAY) is None and sources.book_data(MONDAY) is None


def test_responses_are_cached_and_autofill_can_be_switched_off(web, monkeypatch):
    sources.events_list_data(MONDAY)
    sources.events_list_data(MONDAY)
    assert web.count(f"{RAW}/_data/events.yaml") == 1
    cache.clear()
    monkeypatch.setenv("RELAY_NEWSLETTER_AUTOFILL", "0")
    assert sources.events_list_data(MONDAY) is None
    assert web.count(f"{RAW}/_data/events.yaml") == 1


# --------------------------------------------------------------------------- templates

def test_issue_defaults_continue_numbering(client_record):
    defaults = svc.issue_defaults(client_record, "dtc-weekly", today=dt.date(2026, 11, 12))
    assert defaults == {"key": "weekly-303", "subject": "DataTalks.Club Weekly #303", "preview_text": "",
                        "issue_number": 303, "publication_date": "2026-11-16"}
    # On a Monday the next issue is the following Monday.
    assert svc.issue_defaults(client_record, "dtc-weekly", today=dt.date(2026, 11, 16))["issue_number"] == 304
    svc.create_issue(client_record, {"key": "weekly-310", "subject": "x", "issue_number": 310})
    assert svc.issue_defaults(client_record, "dtc-weekly", today=dt.date(2026, 11, 12))["key"] == "weekly-311"
    assert svc.issue_defaults(client_record, "blank", today=dt.date(2026, 11, 12)) == {
        "key": "", "subject": "", "preview_text": "", "issue_number": None, "publication_date": "2026-11-16"}
    assert [item["key"] for item in svc.newsletter_templates()] == ["dtc-weekly", "blank"]
    with pytest.raises(ApiValidationError):
        svc.issue_defaults(client_record, "nope")


def _weekly(client_record, **extra):
    return svc.create_issue_from_template(client_record, "dtc-weekly", {
        "key": "weekly-303", "subject": "DataTalks.Club Weekly #303", "issue_number": 303,
        "publication_date": "2026-11-16", **extra}, actor="valeriia@datatalks.club")


def test_weekly_template_fills_blocks_from_sources_in_issue_302_order(client_record, web):
    issue, report = _weekly(client_record)
    blocks = list(issue.blocks.order_by("position"))
    assert [(b.block_type, b.needs_review, b.source) for b in blocks] == [
        ("intro", True, "computed"),
        ("sponsor", True, "placeholder"),
        ("course_update", True, "placeholder"),
        ("events_list", False, "website:events"),
        ("recording", True, "website:recordings"),
        ("podcast_episode", True, "website:podcast"),
        ("alexey_article", False, "substack:alexey"),
        ("book_of_week", False, "website:books"),
    ]
    assert issue.template_key == "dtc-weekly"
    assert blocks[0].data == {"issue_label": "#303 · November 16, 2026"}
    assert blocks[1].source_note.startswith("Paste the sponsor's copy")
    assert "Add a 2–3 sentence summary" in blocks[4].source_note
    assert blocks[3].source_note.startswith("Filled automatically from datatalks.club events on ")
    assert [(e["type"], e["status"], e["block_id"]) for e in report] == [
        (b.block_type, "auto" if b.source.startswith(("website:", "substack:")) else "placeholder", b.pk)
        for b in blocks]
    assert all(b.updated_by == "valeriia@datatalks.club" for b in blocks)


def test_weekly_template_without_sources_creates_structure_placeholders(client_record, offline):
    issue, report = _weekly(client_record)
    assert list(issue.blocks.order_by("position").values_list("block_type", "needs_review")) == [
        ("intro", True), ("sponsor", True), ("course_update", True), ("events_list", True)]
    assert [(e["type"], e["status"]) for e in report] == [
        ("intro", "placeholder"), ("sponsor", "placeholder"), ("course_update", "placeholder"),
        ("events_list", "placeholder"), ("recording", "skipped"), ("podcast_episode", "skipped"),
        ("alexey_article", "skipped"), ("book_of_week", "skipped")]
    assert report[3]["note"].startswith("Could not load events from datatalks.club.")
    events = issue.blocks.get(block_type="events_list")
    assert events.source == "website:events" and events.source_note.startswith("No upcoming events were found")


def test_autofill_off_fetches_nothing(client_record, web, monkeypatch):
    issue, report = _weekly(client_record)
    issue.delete()
    web.clear()
    _, report = svc.create_issue_from_template(client_record, "dtc-weekly", {
        "key": "weekly-304", "subject": "x", "publication_date": "2026-11-23"}, autofill=False)
    assert web == []
    assert report[4] == {"block_id": None, "type": "recording", "label": "Recording (workshop / webinar)",
                         "status": "skipped", "note": "Autofill is turned off."}


def test_template_creation_validates_input_before_fetching(client_record, web):
    with pytest.raises(ApiValidationError) as exc:
        svc.create_issue_from_template(client_record, "weekly", {"key": "x", "subject": "x"})
    assert exc.value.errors == {"template": "unknown"}
    with pytest.raises(ApiValidationError):
        svc.create_issue_from_template(client_record, "dtc-weekly", {"key": "x", "subject": "",
                                                                     "publication_date": "2026-11-16"})
    assert web == [] and not NewsletterIssue.objects.exists()
    blank, report = svc.create_issue_from_template(client_record, "blank", {"key": "special", "subject": "Special"})
    assert report == [] and blank.blocks.count() == 0 and blank.template_key == "blank"


# --------------------------------------------------------------------------- drafts, problems, rendering

def test_draft_blocks_skip_required_until_the_editor_saves_them(ready_issue):
    with pytest.raises(ApiValidationError):
        svc.add_block(ready_issue, "sponsor", {})
    block = svc.add_block(ready_issue, "sponsor", {"title": "Half done"}, needs_review=True, source="placeholder",
                          source_note="Paste the copy")
    with pytest.raises(ApiValidationError) as exc:
        svc.add_block(ready_issue, "sponsor", {"cta_url": "javascript:alert(1)"}, needs_review=True)
    assert exc.value.errors == {"data.cta_url": "invalid_url"}
    assert svc.block_problems(block) == ["Needs your review", "Button text is required", "Button link is required"]
    with pytest.raises(ApiValidationError):
        svc.update_block(block, {"title": "Still half done"})
    block.refresh_from_db()
    assert block.needs_review is True
    svc.update_block(block, {"title": "Acme", "cta_text": "Go", "cta_url": "https://acme.dev"})
    block.refresh_from_db()
    assert block.needs_review is False and block.source == "placeholder" and svc.block_problems(block) == []


def test_issue_problems_lists_unfinished_blocks(client_record, ready_issue):
    assert svc.issue_problems(ready_issue) == []
    pending = svc.add_block(ready_issue, "events_list", {}, needs_review=True)
    svc.add_block(ready_issue, "html", {"html": "<p>Hi {{ name }}</p>"})
    problems = svc.issue_problems(ready_issue)
    assert [(p["block_id"], p["position"], p["label"]) for p in problems] == [
        (pending.pk, 2, "Upcoming events list"), (ready_issue.blocks.get(block_type="html").pk, 3, "Custom HTML")]
    assert problems[0]["problems"] == ["Needs your review", "Events is required"]
    assert "merge tags" in problems[1]["problems"][0]
    empty = svc.create_issue(client_record, {"key": "empty", "subject": "Empty"})
    assert svc.issue_problems(empty) == [{"block_id": None, "position": None, "label": "Issue",
                                          "problems": ["The issue has no blocks yet"]}]
    assert svc.issue_payload(ready_issue)["problems"] == 2


def test_mark_unfinished_flags_placeholders_only_in_editor_previews(ready_issue):
    svc.add_block(ready_issue, "course_update", {}, needs_review=True, source="placeholder",
                  source_note="Course news <for> this week")
    plain = svc.render_issue(ready_issue)["html_body"]
    marked = svc.render_issue(ready_issue, mark_unfinished=True)["html_body"]
    assert "Needs content" not in plain
    assert "Needs content:</strong> Course update / preparation card — Course news &lt;for&gt; this week" in marked
    # An empty block adds no section, so no doubled divider.
    assert plain.count(svc._DIVIDER) == 2
    assert marked.count(svc._DIVIDER) == 3


def test_partial_data_renders_without_crashing():
    for block_type in svc.BLOCK_TYPES:
        svc.render_block_html(block_type, {})
        svc.render_block_text(block_type, {})
    html = svc.render_block_html("link_list", {"links": "Only a title\nGood | https://x.dev | note"})
    assert "Only a title" in html and 'href="https://x.dev"' in html
    assert svc.render_block_html("nope", {}) == ""


def test_change_type_keeps_shared_fields_and_duplicate_inserts_after(ready_issue):
    sponsor = ready_issue.blocks.get(block_type="sponsor")
    sponsor.data = {**sponsor.data, "kicker": "SPONSORED"}
    sponsor.save()
    svc.change_block_type(sponsor, "course_promo")
    sponsor.refresh_from_db()
    assert sponsor.block_type == "course_promo" and sponsor.needs_review is True
    assert sponsor.data == {"title": "Try Acme", "cta_text": "Try it", "cta_url": "https://acme.dev/"}
    svc.change_block_type(sponsor, "events_list")
    sponsor.refresh_from_db()
    assert sponsor.data == {"kicker": "EVENTS", "title": "Try Acme"}
    with pytest.raises(ApiValidationError):
        svc.change_block_type(sponsor, "bogus")
    copy = svc.duplicate_block(ready_issue.blocks.get(block_type="intro"))
    assert list(ready_issue.blocks.order_by("position").values_list("block_type", "position")) == [
        ("intro", 0), ("intro", 1), ("events_list", 2)]
    assert copy.position == 1 and copy.data["summary"] == "This week: **launch**."


def test_refill_replaces_source_data_and_keeps_the_editors_summary(client_record, web, monkeypatch):
    issue, _ = _weekly(client_record)
    recording = issue.blocks.get(block_type="recording")
    svc.update_block(recording, {**recording.data, "title": recording.data["title"], "meta": "edited",
                                 "body": "We built an agent."})
    svc.refill_block(recording)
    recording.refresh_from_db()
    assert recording.data["meta"] == "Webinar · Nov 10 · with Alexey Grigorev and Mikhail Rozhkov"
    assert recording.data["body"] == "We built an agent." and recording.needs_review is False
    with pytest.raises(ApiValidationError) as exc:
        svc.refill_block(issue.blocks.get(block_type="sponsor"))
    assert exc.value.errors == {"block": "no_source"}
    cache.clear()
    monkeypatch.setattr(sources, "_http_get", lambda url: None)
    with pytest.raises(ApiValidationError) as exc:
        svc.refill_block(issue.blocks.get(block_type="events_list"))
    assert exc.value.errors == {"block": "source_unavailable"}


# --------------------------------------------------------------------------- campaign assembly

@pytest.fixture
def no_queueing(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("assembling a newsletter must never queue a campaign")

    monkeypatch.setattr(campaign_services, "queue_campaign", forbidden)
    monkeypatch.setattr(campaign_services, "request_campaign_queue", forbidden)


def _assert_nothing_sent(campaign):
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.DRAFT and campaign.scheduled_at is None
    assert not CampaignRecipient.objects.filter(campaign=campaign).exists()
    assert not CampaignDispatch.objects.exists()
    assert not DBTaskResult.objects.exists()


def test_assemble_creates_then_updates_one_draft_campaign(ready_issue, audience, no_queueing):
    campaign, created = svc.assemble_campaign(ready_issue, audience="datatalks-club", actor="valeriia@datatalks.club")
    assert created is True
    assert (campaign.external_key, campaign.subject, campaign.preview_text) == (
        "weekly-303", "DataTalks.Club Weekly #303", "Course launch")
    assert campaign.audience == audience and campaign.client == ready_issue.client
    assert "Try Acme" in campaign.html_body and "Try it: https://acme.dev/" in campaign.text_body
    assert "Needs content" not in campaign.html_body and "TEST EMAIL" not in campaign.html_body
    assert campaign.metadata["newsletter"]["issue_id"] == ready_issue.pk
    assert campaign.metadata["newsletter"]["assembled_by"] == "valeriia@datatalks.club"
    assert "operator_editor" not in campaign.metadata
    assert campaign_services.campaign_send_issues(campaign) == []
    ready_issue.refresh_from_db()
    assert ready_issue.campaign == campaign and svc.campaign_newsletter_issue(campaign) == ready_issue
    _assert_nothing_sent(campaign)

    campaign.metadata = {**campaign.metadata, "operator_editor": {"version": 1, "blocks": []}, "keep": 1}
    campaign.save()
    svc.update_block(ready_issue.blocks.get(block_type="sponsor"),
                     {"title": "Try Acme 2", "cta_text": "Try it", "cta_url": "https://acme.dev/"})
    again, created = svc.assemble_campaign(ready_issue, audience=audience)
    assert created is False and again.pk == campaign.pk and Campaign.objects.count() == 1
    assert "Try Acme 2" in again.html_body
    assert "operator_editor" not in again.metadata and again.metadata["keep"] == 1
    _assert_nothing_sent(again)


def test_assemble_refuses_unfinished_issues_and_foreign_audiences(client_record, ready_issue, foreign_audience):
    with pytest.raises(ApiValidationError) as exc:
        svc.assemble_campaign(ready_issue, audience=foreign_audience)
    assert exc.value.errors == {"audience": "invalid"}
    with pytest.raises(ApiValidationError):
        svc.assemble_campaign(ready_issue, audience="aishl-members")
    svc.add_block(ready_issue, "sponsor", {}, needs_review=True)
    with pytest.raises(ApiValidationError) as exc:
        svc.assemble_campaign(ready_issue, audience="datatalks-club")
    assert exc.value.errors == {"issue": "has_problems"}
    assert not Campaign.objects.exists()


def test_assemble_refuses_sent_campaigns_and_key_clashes(client_record, ready_issue, audience):
    Campaign.objects.create(client=client_record, audience=audience, external_key="weekly-303", subject="Old")
    with pytest.raises(ApiValidationError) as exc:
        svc.assemble_campaign(ready_issue, audience=audience)
    assert exc.value.errors == {"campaign": "key_in_use"} and exc.value.status_code == 409
    Campaign.objects.all().delete()
    campaign, _ = svc.assemble_campaign(ready_issue, audience=audience)
    Campaign.objects.filter(pk=campaign.pk).update(status=CampaignStatus.SENT)
    ready_issue.refresh_from_db()
    with pytest.raises(ApiValidationError) as exc:
        svc.assemble_campaign(ready_issue, audience=audience)
    assert exc.value.errors == {"campaign": "not_draft"} and exc.value.status_code == 409


# --------------------------------------------------------------------------- API

@pytest.fixture
def api(client_record):
    _, raw = create_client_api_key(client=client_record, name="dataops")
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def _json(response):
    return json.loads(response.content)


def test_api_template_create_refill_and_assemble(api, client_record, audience, web, no_queueing):
    http = HttpClient()
    assert [t["key"] for t in _json(http.get("/api/newsletter-templates", **api))["templates"]] == ["dtc-weekly",
                                                                                                     "blank"]
    response = http.put("/api/newsletter-issues/weekly-303", json.dumps({
        "template": "dtc-weekly", "subject": "DataTalks.Club Weekly #303", "issue_number": 303,
        "publication_date": "2026-11-16"}), content_type="application/json", **api)
    assert response.status_code == 201
    body = _json(response)
    assert [e["status"] for e in body["autofill"]][:4] == ["placeholder", "placeholder", "placeholder", "auto"]
    assert body["issue"]["template"] == "dtc-weekly" and body["issue"]["campaign"] is None
    assert body["issue"]["blocks"][0]["needs_review"] is True and body["issue"]["problems"] > 0
    events_id = next(b["id"] for b in body["issue"]["blocks"] if b["type"] == "events_list")
    response = http.post(f"/api/newsletter-issues/weekly-303/blocks/{events_id}/refill", "{}",
                         content_type="application/json", **api)
    assert response.status_code == 200 and _json(response)["block"]["source"] == "website:events"

    response = http.post("/api/newsletter-issues/weekly-303/assemble", json.dumps({"audience": "datatalks-club"}),
                         content_type="application/json", **api)
    assert response.status_code == 400 and _json(response)["error"]["fields"] == {"issue": "has_problems"}
    issue = NewsletterIssue.objects.get(key="weekly-303")
    for block in issue.blocks.filter(needs_review=True):
        block.delete()
    response = http.post("/api/newsletter-issues/weekly-303/blocks",
                         json.dumps({"type": "intro", "data": {}, "needs_review": True, "position": 0}),
                         content_type="application/json", **api)
    assert response.status_code == 201 and _json(response)["block"]["needs_review"] is True
    issue.blocks.get(block_type="intro").delete()
    response = http.post("/api/newsletter-issues/weekly-303/assemble", json.dumps({"audience": "datatalks-club"}),
                         content_type="application/json", **api)
    assert response.status_code == 201
    payload = _json(response)
    assert payload["created"] is True and payload["campaign"]["status"] == "draft"
    assert payload["campaign"]["external_key"] == "weekly-303"
    assert _json(http.get("/api/newsletter-issues/weekly-303", **api))["issue"]["campaign"]["status"] == "draft"
    _assert_nothing_sent(Campaign.objects.get())


def test_api_rejects_unknown_template_and_other_clients(api, client_record, ready_issue, audience):
    http = HttpClient()
    response = http.put("/api/newsletter-issues/weekly-999", json.dumps({"template": "nope", "subject": "x"}),
                        content_type="application/json", **api)
    assert response.status_code == 400 and _json(response)["error"]["fields"] == {"template": "unknown"}
    assert http.get("/api/newsletter-templates").status_code == 401
    assert http.post("/api/newsletter-issues/weekly-303/assemble", "{}", content_type="application/json").status_code \
        == 401
    other = Client.objects.create(organization=client_record.organization, name="Other", slug="other")
    _, raw = create_client_api_key(client=other, name="other")
    foreign = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
    block = ready_issue.blocks.first()
    assert http.post("/api/newsletter-issues/weekly-303/assemble", json.dumps({"audience": "datatalks-club"}),
                     content_type="application/json", **foreign).status_code == 404
    assert http.post(f"/api/newsletter-issues/weekly-303/blocks/{block.pk}/refill", "{}",
                     content_type="application/json", **foreign).status_code == 404
    assert not Campaign.objects.exists()
    assert NewsletterBlock.objects.filter(issue=ready_issue).count() == 2
