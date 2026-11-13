"""The block flow must produce the DataTalks.Club Weekly email we sent before.

``fixtures/newsletter_302_sent.html`` is issue #302 exactly as it went out on
2026-11-09 (Relay campaign 18). ``newsletter_302_blocks`` re-enters that issue
as blocks; rendering them must give the same text, images, links, buttons,
sections and footer.
"""

import html as html_lib
import re
from pathlib import Path

import pytest

from mailing.models import Client, Organization
from mailing.services import newsletter_layout as layout
from mailing.services import newsletters as svc
from mailing.tests import newsletter_302_blocks as fx

SENT = (Path(__file__).parent / "fixtures" / "newsletter_302_sent.html").read_text(encoding="utf-8")


@pytest.fixture
def issue_302(db):
    org = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    client = Client.objects.create(organization=org, slug="dtc-newsletter", name="DTC Newsletter")
    issue = svc.create_issue(client, {"key": "weekly-302", "subject": fx.SUBJECT, "preview_text": "",
                                      "issue_number": 302, "publication_date": "2026-11-09"})
    for block_type, data in fx.BLOCKS:
        svc.add_block(issue, block_type, data)
    return issue


def _visible_text(html):
    html = re.sub(r"<!--\[if mso\]>.*?<!\[endif\]-->", "", html, flags=re.S)
    html = re.sub(r"<(style|title)[^>]*>.*?</\1>", "", html, flags=re.S)
    html = re.sub(r'<div style="display: none; max-height: 0px; overflow: hidden;">.*?</div>', "", html, flags=re.S)
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", html))
    return re.sub(r"\s+", " ", text).strip()


def _images(html):
    return re.findall(r'<img[^>]*src="([^"]+)"', html)


def _links(html):
    return [html_lib.unescape(url) for url in re.findall(r'<a href="([^"]+)"', html)]


def _section_backgrounds(html):
    return re.findall(r'<td style="background-color:(#[0-9A-Fa-f]{6})" valign="top" class="mceWrapperInner"', html)


def test_302_from_blocks_matches_what_we_sent(issue_302):
    rendered = svc.render_issue(issue_302)["html_body"]
    assert _visible_text(rendered) == _visible_text(SENT)
    assert _images(rendered) == _images(SENT)
    assert _links(rendered) == _links(SENT)
    assert re.findall(r'class="mceButtonLink"[^>]*>([^<]+)</a>', rendered) == \
        re.findall(r'class="mceButtonLink"[^>]*>([^<]+)</a>', SENT)
    # Same section structure: white header, lavender promo card, white sections, white footer.
    assert [color.upper() for color in _section_backgrounds(rendered)][:2] == ["#FFFFFF", "#ECECFB"]
    assert {color.upper() for color in _section_backgrounds(rendered)} == {"#FFFFFF", "#ECECFB"}
    # Head (CSS) and footer are the sent email's, byte for byte.
    assert rendered[:rendered.index("<title>")] == SENT[:SENT.index("<title>")]
    footer = SENT[SENT.index('<tbody data-block-id="30" class="mceWrapper">'):]
    assert rendered.endswith(footer)


def test_302_preheader_falls_back_to_the_intro_like_before(issue_302):
    rendered = svc.render_issue(issue_302)["html_body"]
    preheader = re.search(r'class="mcnPreviewText"[^>]*>(.*?)</span>', rendered, re.S).group(1)
    assert preheader.startswith(html_lib.escape(fx.PREVIEW, quote=False))
    assert svc.effective_preview_text(issue_302) == fx.PREVIEW


def test_302_text_part_keeps_the_sent_footer(issue_302):
    text = svc.render_issue(issue_302)["text_body"]
    assert text.startswith("https://datatalks.club/\n\n#302 · November 9, 2026")
    assert "Register now (https://courses.datatalks.club/register/production-ai-agents-generative-media/)" in text
    assert text.endswith(SENT_TEXT_FOOTER)


SENT_TEXT_FOOTER = ("Copyright (C) 2026 DataTalks. All rights reserved.\n\nOur mailing address is:\n"
                    "DataTalks, Langhansstr. 70, Berlin, 13086, DE\n\nWant to change how you receive these emails?\n")


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
