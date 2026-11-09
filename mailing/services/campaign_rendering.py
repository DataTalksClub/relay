"""Pure campaign body rendering and action-link rewriting."""

import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlparse

from mailing.services.public_urls import click_redirect_url, open_pixel_url, unsubscribe_url

# Mailchimp archive copies keep recipient-specific footer links with a literal [UNIQID] placeholder.
_UNIQID = "[UNIQID]"
_FORWARD_LINK_RE = re.compile(
    r"<a\b[^>]*href=\"[^\"]*forward-to-friend\.com[^\"]*\"[^>]*>.*?</a>(?:\s*(?:&middot;|&#183;|·)\s*)?",
    flags=re.IGNORECASE | re.DOTALL,
)
_MAILCHIMP_ACCOUNT_LINK_RE = re.compile(
    r"^https?://[^/]*list-manage\.com/(?:unsubscribe|profile)\b", flags=re.IGNORECASE
)
_TEXT_FORWARD_LINE_RE = re.compile(r"^.*forward-to-friend\.com.*(?:\n|$)", flags=re.IGNORECASE | re.MULTILINE)
_TEXT_ACCOUNT_URL_RE = re.compile(
    r"https?://[^\s)>\"]*list-manage\.com/(?:unsubscribe|profile)[^\s)>\"]*", flags=re.IGNORECASE
)


def resolve_archive_placeholders_html(html_body):
    """Drop forward-to-a-friend (Relay has no such feature); the rewriter handles the other links."""
    return _FORWARD_LINK_RE.sub("", html_body)


def resolve_archive_placeholders_text(text_body, unsubscribe_token):
    text = _TEXT_FORWARD_LINE_RE.sub("", text_body)
    text = _TEXT_ACCOUNT_URL_RE.sub(lambda _: unsubscribe_url(unsubscribe_token), text)
    return text.replace("?e=[UNIQID]", "").replace("&e=[UNIQID]", "")


def build_campaign_html_body(html_body, tracking_token, unsubscribe_token):
    body = rewrite_html_links(resolve_archive_placeholders_html(html_body or ""), tracking_token, unsubscribe_token)
    pixel = f'<img src="{escape(open_pixel_url(tracking_token), quote=True)}" width="1" height="1" alt="" />'
    unsubscribe_href = escape(unsubscribe_url(unsubscribe_token), quote=True)
    footer = f'<p><a href="{unsubscribe_href}">Unsubscribe or manage preferences</a></p>'
    additions = f"\n{footer}\n{pixel}\n"
    closing_body = list(re.finditer(r"</body\s*>", body, flags=re.IGNORECASE))
    if closing_body:
        position = closing_body[-1].start()
        return body[:position] + additions + body[position:]
    return f"{body}\n{footer}\n{pixel}"


def build_campaign_text_body(text_body, unsubscribe_token):
    url = unsubscribe_url(unsubscribe_token)
    body = resolve_archive_placeholders_text(text_body or "", unsubscribe_token).rstrip()
    if body:
        return f"{body}\n\nUnsubscribe or manage preferences: {url}"
    return f"Unsubscribe or manage preferences: {url}"


def campaign_list_id(campaign, host):
    """RFC 2919 List-Id, unique per client + audience, e.g. ``DataTalks.Club <dtc.news.list-id.relay.example>``."""
    name = " ".join(re.sub(r"[<>\"\\\r\n]+", " ", campaign.audience.name).split())
    list_id = f"{campaign.client.slug}.{campaign.audience.slug}.list-id.{host}"
    return f"{name} <{list_id}>" if name else f"<{list_id}>"


def campaign_unsubscribe_headers(unsubscribe_token, campaign=None):
    """Build RFC 8058 one-click headers (and the per-list List-Id) when Relay has a public HTTPS URL."""
    url = unsubscribe_url(unsubscribe_token)
    parsed_url = urlparse(url)
    if parsed_url.scheme.lower() != "https" or not parsed_url.netloc:
        return {}
    headers = {
        "List-Unsubscribe": f"<{url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    if campaign is not None:
        headers["List-Id"] = campaign_list_id(campaign, parsed_url.hostname)
    return headers


def rewrite_html_links(html_body, tracking_token, unsubscribe_token=None):
    rewriter = _ClickTrackingHTMLRewriter(tracking_token, unsubscribe_token)
    rewriter.feed(html_body)
    rewriter.close()
    return rewriter.output


class _ClickTrackingHTMLRewriter(HTMLParser):
    def __init__(self, tracking_token, unsubscribe_token=None):
        super().__init__(convert_charrefs=False)
        self.tracking_token = tracking_token
        self.unsubscribe_token = unsubscribe_token
        self.parts = []

    @property
    def output(self):
        return "".join(self.parts)

    def handle_starttag(self, tag, attrs):
        self.parts.append(self._format_tag(tag, attrs, closed=False))

    def handle_startendtag(self, tag, attrs):
        self.parts.append(self._format_tag(tag, attrs, closed=True))

    def handle_endtag(self, tag):
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data):
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.parts.append(f"<?{data}>")

    def _format_tag(self, tag, attrs, *, closed):
        rewritten_attrs = []
        for name, value in attrs:
            if tag.lower() == "a" and name.lower() == "href" and value and _UNIQID in value:
                if self.unsubscribe_token and _MAILCHIMP_ACCOUNT_LINK_RE.match(value):
                    # Relay's unsubscribe page is the single unsubscribe/preferences surface; skip click tracking.
                    rewritten_attrs.append((name, unsubscribe_url(self.unsubscribe_token)))
                    continue
                value = value.replace("?e=[UNIQID]", "").replace("&e=[UNIQID]", "")
            if tag.lower() == "a" and name.lower() == "href" and _is_trackable_url(value):
                value = click_redirect_url(self.tracking_token, value)
            rewritten_attrs.append((name, value))

        attr_text = "".join(_format_attr(name, value) for name, value in rewritten_attrs)
        suffix = ">"
        if closed:
            suffix = " />"
        return f"<{tag}{attr_text}{suffix}"


def _format_attr(name, value):
    if value is None:
        return f" {name}"
    return f' {name}="{escape(value, quote=True)}"'


def _is_trackable_url(value):
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
