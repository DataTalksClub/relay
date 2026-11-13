"""The block flow must produce the DataTalks.Club Weekly email we sent before.

``fixtures/newsletter_302_sent.html`` is issue #302 as Relay campaign 18 sent
it on 2026-11-09. ``newsletter_302_blocks`` re-enters that issue as blocks;
rendering them must give the same text, images, links, buttons and sections.
The header and footer follow the Mailchimp-sent #301/#300/#299 and the #302
archive: "View this email in your browser", "Forward to a friend", Mailchimp
Subscribe/Past issues, and "You can update your preferences or unsubscribe".
"""

import html as html_lib
import re
from pathlib import Path

import pytest

from mailing.models import Client, Organization
from mailing.services import campaign_rendering
from mailing.services import newsletter_layout as layout
from mailing.services import newsletters as svc
from mailing.tests import newsletter_302_blocks as fx

SENT = (Path(__file__).parent / "fixtures" / "newsletter_302_sent.html").read_text(encoding="utf-8")


@pytest.fixture
def issue_302(db):
    org = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    client = Client.objects.create(organization=org, slug="dtc-newsletter", name="DTC Newsletter")
    issue = svc.create_issue(client, {"key": "weekly-302", "subject": fx.SUBJECT, "preview_text": fx.PREHEADER,
                                      "issue_number": 302, "publication_date": "2026-11-09"})
    for block_type, data in fx.BLOCKS:
        svc.add_block(issue, block_type, data)
    return issue


def _visible_text(html):
    html = re.sub(r"<!--\[if mso\]>.*?<!\[endif\]-->", "", html, flags=re.S)
    html = re.sub(r"<(style|title)[^>]*>.*?</\1>", "", html, flags=re.S)
    html = re.sub(r'<span class="mcnPreviewText"[^>]*>.*?</span>', "", html, flags=re.S)  # checked separately
    html = re.sub(r'<div style="display: none; max-height: 0px; overflow: hidden;">.*?</div>', "", html, flags=re.S)
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", html))
    return re.sub(r"\s+", " ", text).strip()


def _images(html):
    return re.findall(r'<img[^>]*src="([^"]+)"', html)


def _links(html):
    return [html_lib.unescape(url) for url in re.findall(r'<a href="([^"]+)"', html)]


def _section_backgrounds(html):
    return re.findall(r'<td style="background-color:(#[0-9A-Fa-f]{6})" valign="top" class="mceWrapperInner"', html)


SUBSCRIBE = "https://club.us19.list-manage.com/subscribe?u=0d7822ab98152f5afc118c176&id=97178021aa"
PAST_ISSUES = "https://us19.campaign-archive.com/home/?u=0d7822ab98152f5afc118c176&id=97178021aa"
VIEW_LINE = "View this email in your browser"
UNSUB_LINE = "You can update your preferences or unsubscribe"


def _without_301_parts(text):
    for part in (VIEW_LINE, "Forward to a friend · ", " " + UNSUB_LINE):
        text = text.replace(part, "")
    return re.sub(r"\s+", " ", text).strip()


def test_302_from_blocks_matches_what_we_sent(issue_302):
    rendered = svc.render_issue(issue_302)["html_body"]
    assert _without_301_parts(_visible_text(rendered)) == _visible_text(SENT)
    assert _images(rendered) == _images(SENT)
    # Same links as campaign 18, apart from the #301 header/footer links it lacked.
    old_footer = {SUBSCRIBE: "https://datatalks.club/", PAST_ISSUES: "https://datatalks.club/blog/"}
    content = [old_footer.get(url, url) for url in _links(rendered)
               if not url.startswith(("http://localhost", "mailto:?")) and "[UNIQID]" not in url]
    assert content == _links(SENT)
    assert re.findall(r'class="mceButtonLink"[^>]*>([^<]+)</a>', rendered) == \
        re.findall(r'class="mceButtonLink"[^>]*>([^<]+)</a>', SENT)
    # Same section structure: white header, lavender promo card, white sections, white footer.
    assert [color.upper() for color in _section_backgrounds(rendered)][:2] == ["#FFFFFF", "#ECECFB"]
    assert {color.upper() for color in _section_backgrounds(rendered)} == {"#FFFFFF", "#ECECFB"}
    # Head (CSS) is the sent email's, byte for byte; the footer keeps the address and social icons.
    assert rendered[:rendered.index("<title>")] == SENT[:SENT.index("<title>")]
    assert "DataTalks, Langhansstr. 70, Berlin, 13086, DE" in rendered
    for social in ("https://twitter.com/datatalksclub", "https://linkedin.com/company/datatalks-club",
                   "https://www.youtube.com/c/DataTalksClub", "mailto:alexey@datatalks.club"):
        assert f'href="{social}"' in rendered


def test_302_header_and_footer_match_the_mailchimp_sent_issues(issue_302):
    rendered = svc.render_issue(issue_302)["html_body"]
    text = _visible_text(rendered)
    # Top right, above the logo, like #301/#300/#299 and the #302 archive.
    view = re.search(r'<p style="text-align: right;" class="last-child"><a href="([^"]+)"><span style="font-size: '
                     r'14px">View this email in your browser</span></a></p>', rendered)
    assert view and rendered.index(VIEW_LINE) < rendered.index("bf262d0f-68be-b981-422b-e8e5cfc8eeca.png")
    assert html_lib.unescape(view.group(1)) == svc.web_view_url(issue_302)
    assert "Forward to a friend · Subscribe · Past issues · Sponsor this newsletter" in text
    links = _links(rendered)
    assert SUBSCRIBE in links and PAST_ISSUES in links
    assert "https://datatalks.club/blog/" not in links
    forward = next(url for url in links if url.startswith("mailto:?"))
    assert svc.web_view_url(issue_302).replace(":", "%3A").replace("/", "%2F") in forward
    assert "Want to change how you receive these emails? " + UNSUB_LINE in text
    assert any("list-manage.com/profile?" in url and "[UNIQID]" in url for url in links)
    assert any("list-manage.com/unsubscribe?" in url and "[UNIQID]" in url for url in links)
    # A real render has no test banner.
    assert "TEST EMAIL" not in rendered and "fff4ce" not in rendered


def test_real_send_points_preferences_and_unsubscribe_at_relay(issue_302):
    rendered = svc.render_issue(issue_302)
    html = campaign_rendering.build_campaign_html_body(rendered["html_body"], "trk", "UNSUBTOKEN")
    assert html.count('href="http://localhost:8000/unsubscribe/UNSUBTOKEN"') >= 2
    assert "list-manage.com/profile" not in html and "list-manage.com/unsubscribe" not in html
    assert "You can </span><a ses:no-track href=\"http://localhost:8000/unsubscribe/UNSUBTOKEN\">" in html
    assert "mailto:?" in html  # Forward to a friend survives the send rewrite
    text = campaign_rendering.build_campaign_text_body(rendered["text_body"], "UNSUBTOKEN")
    assert "update your preferences (http://localhost:8000/unsubscribe/UNSUBTOKEN)" in text
    assert "[UNIQID]" not in text


def test_test_send_keeps_the_sentence_but_disables_recipient_links(issue_302):
    rendered = svc.render_issue(issue_302, test=True)
    assert rendered["subject"].startswith("[TEST] ")
    assert "TEST EMAIL" in rendered["html_body"]
    assert UNSUB_LINE in _visible_text(rendered["html_body"])
    assert "[UNIQID]" not in rendered["html_body"] and "[UNIQID]" not in rendered["text_body"]
    real = svc.render_issue(issue_302)
    assert not real["subject"].startswith("[TEST]") and "TEST EMAIL" not in real["text_body"]


def test_302_preheader_is_the_issue_preheader_not_the_intro(issue_302):
    rendered = svc.render_issue(issue_302)["html_body"]
    preheader = re.search(r'class="mcnPreviewText"[^>]*>(.*?)</span>', rendered, re.S).group(1)
    assert preheader == html_lib.escape(fx.PREHEADER, quote=False)
    assert svc.effective_preview_text(issue_302) == fx.PREHEADER
    issue_302.preview_text = ""
    issue_302.save()
    rendered = svc.render_issue(issue_302)["html_body"]
    assert re.search(r'class="mcnPreviewText"[^>]*>(.*?)</span>', rendered, re.S).group(1) == ""
    assert svc.effective_preview_text(issue_302) == ""


def test_302_text_part_keeps_the_sent_footer(issue_302):
    text = svc.render_issue(issue_302)["text_body"]
    assert text.startswith(f"View this email in your browser ({svc.web_view_url(issue_302)})\n\n"
                           "https://datatalks.club/\n\n#302 · November 9, 2026")
    assert "Register now (https://courses.datatalks.club/register/production-ai-agents-generative-media/)" in text
    assert f"Subscribe ({SUBSCRIBE}) · Past issues ({PAST_ISSUES})" in text
    assert SENT_TEXT_FOOTER in text
    assert "You can update your preferences (" in text


SENT_TEXT_FOOTER = ("Copyright (C) 2026 DataTalks. All rights reserved.\n\nOur mailing address is:\n"
                    "DataTalks, Langhansstr. 70, Berlin, 13086, DE\n\nWant to change how you receive these emails?\n")


def test_web_view_serves_the_issue_publicly_with_a_signed_link(issue_302, client):
    url = svc.web_view_url(issue_302).removeprefix("http://localhost:8000")
    response = client.get(url)
    assert response.status_code == 200
    body = response.content.decode()
    assert "Production AI Agents for Generative Media 2026 Cohort" in body and "TEST EMAIL" not in body
    assert "[UNIQID]" not in body and UNSUB_LINE in _visible_text(body)
    assert response["Content-Security-Policy"].startswith("sandbox")
    assert client.get(url[:-2] + "xx").status_code == 404
    assert client.get(f"/newsletter/view/{issue_302.pk}").status_code == 404


def test_layout_sections_follow_302_rules():
    promo, course, events = ("course_promo", "<tr>p</tr>"), ("course_update", "<tr>c</tr>"), ("events_list", "<tr>e</tr>")
    html = layout.sections_html([promo, course, course, events])
    # Lavender promo card, then one white section holding both course cards (no divider
    # between them), then the events section after a divider, then the closing divider.
    assert html.count(f"background-color:{layout.PROMO_BACKGROUND}") == 1
    assert html.count(layout.DIVIDER_ROW) == 3
    assert layout.COURSE_GAP_ROW + "<tr>c</tr>" in html
    assert layout.sections_html([]) == ""


def test_markdown_uses_the_302_text_conventions():
    parts = layout.markdown_blocks("First.\n\nIntro:\n\n- one\n- [two](https://x.dev)\n\nLast <b>x</b>.")
    assert layout.join_text(parts) == (
        '<p>First.</p><p><br/></p><p>Intro:</p><ul><li><p>one</p></li><li><p><a href="https://x.dev" '
        'target="_blank">two</a></p></li></ul><p><br/></p><p class="last-child">Last &lt;b&gt;x&lt;/b&gt;.</p>')
    assert 'href="#"' in layout.join_text(layout.markdown_blocks("[bad](javascript:alert(1))"))
