import re

from mailing.services.campaign_rendering import build_campaign_html_body, rewrite_html_links


def _anchors(html):
    return re.findall(r"<a\b[^>]*>", html)


def test_rewritten_links_carry_ses_no_track_and_relay_redirect():
    html = build_campaign_html_body('<p><a href="https://datatalks.club/">x</a></p>', "tok", "unsub")

    tracked, footer = _anchors(html)
    assert tracked.startswith("<a ses:no-track href=")
    assert "/t/c/tok?" in tracked
    assert footer.startswith("<a ses:no-track href=")


def test_non_http_links_are_not_marked():
    out = rewrite_html_links('<a href="mailto:a@b.c">m</a><a name="top">t</a>', "tok")

    assert "ses:no-track" not in out


def test_existing_no_track_attribute_is_not_duplicated():
    out = rewrite_html_links('<a ses:no-track href="https://datatalks.club/">x</a>', "tok")

    assert out.count("ses:no-track") == 1
