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


def resolve_archive_placeholders_text(text_body, unsubscribe_token, unsubscribe_template=""):
    text = _TEXT_FORWARD_LINE_RE.sub("", text_body)
    text = _TEXT_ACCOUNT_URL_RE.sub(lambda _: unsubscribe_url(unsubscribe_token, unsubscribe_template), text)
    return text.replace("?e=[UNIQID]", "").replace("&e=[UNIQID]", "")


# Relay counts clicks itself through /t/c/. SES strips this attribute before delivery and then
# leaves the link alone, so it does not wrap it in awstrack.me or record a second click.
_SES_NO_TRACK = "ses:no-track"

_DOWNLEVEL_REVEALED_RE = re.compile(r"^\[(?:if[^\]]*|endif)\]$", flags=re.IGNORECASE)
_CONDITIONAL_COMMENT_RE = re.compile(
    r"(?P<open>\[if[^\]]*\]>)(?P<body>.*?)(?P<close><!\[endif\])$",
    flags=re.IGNORECASE | re.DOTALL,
)


def build_campaign_html_body(html_body, tracking_token, unsubscribe_token, unsubscribe_template=""):
    body = rewrite_html_links(
        resolve_archive_placeholders_html(html_body or ""), tracking_token, unsubscribe_token, unsubscribe_template
    )
    pixel = f'<img src="{escape(open_pixel_url(tracking_token), quote=True)}" width="1" height="1" alt="" />'
    unsubscribe_href = escape(unsubscribe_url(unsubscribe_token, unsubscribe_template), quote=True)
    footer = f'<p><a {_SES_NO_TRACK} href="{unsubscribe_href}">Unsubscribe or manage preferences</a></p>'
    additions = f"\n{footer}\n{pixel}\n"
    closing_body = list(re.finditer(r"</body\s*>", body, flags=re.IGNORECASE))
    if closing_body:
        position = closing_body[-1].start()
        return body[:position] + additions + body[position:]
    return f"{body}\n{footer}\n{pixel}"


def build_campaign_text_body(text_body, unsubscribe_token, unsubscribe_template=""):
    url = unsubscribe_url(unsubscribe_token, unsubscribe_template)
    body = resolve_archive_placeholders_text(text_body or "", unsubscribe_token, unsubscribe_template).rstrip()
    if body:
        return f"{body}\n\nUnsubscribe or manage preferences: {url}"
    return f"Unsubscribe or manage preferences: {url}"


def campaign_list_id(campaign, host):
    """RFC 2919 List-Id, unique per client + audience, e.g. ``DataTalks.Club <dtc.news.list-id.relay.example>``."""
    name = " ".join(re.sub(r"[<>\"\\\r\n]+", " ", campaign.audience.name).split())
    list_id = f"{campaign.client.slug}.{campaign.audience.slug}.list-id.{host}"
    return f"{name} <{list_id}>" if name else f"<{list_id}>"


def campaign_unsubscribe_headers(unsubscribe_token, campaign=None):
    """Build RFC 8058 one-click headers (and the per-list List-Id) when the unsubscribe URL is public HTTPS.

    A campaign carrying ``unsubscribe_url_template`` points the headers at the sender's own endpoint.
    """
    template = campaign.unsubscribe_url_template if campaign is not None else ""
    url = unsubscribe_url(unsubscribe_token, template)
    parsed_url = urlparse(url)
    if parsed_url.scheme.lower() != "https" or not parsed_url.netloc:
        return {}
    headers = {
        "List-Unsubscribe": f"<{url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    if campaign is not None:
        # List-Id stays on Relay's host: it identifies the Relay audience, not the unsubscribe endpoint.
        relay_host = urlparse(unsubscribe_url(unsubscribe_token)).hostname or parsed_url.hostname
        headers["List-Id"] = campaign_list_id(campaign, relay_host)
    return headers


def rewrite_html_links(html_body, tracking_token, unsubscribe_token=None, unsubscribe_template=""):
    rewriter = _ClickTrackingHTMLRewriter(tracking_token, unsubscribe_token, unsubscribe_template)
    rewriter.feed(html_body)
    rewriter.close()
    return rewriter.output


class _ClickTrackingHTMLRewriter(HTMLParser):
    def __init__(self, tracking_token, unsubscribe_token=None, unsubscribe_template=""):
        super().__init__(convert_charrefs=False)
        self.tracking_token = tracking_token
        self.unsubscribe_token = unsubscribe_token
        self.unsubscribe_template = unsubscribe_template
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
        # Outlook conditional comments (<!--[if mso]>...<![endif]-->) hold real HTML that Outlook renders and
        # SES still scans, so their links need the same rewrite and no-track mark as the visible body.
        match = _CONDITIONAL_COMMENT_RE.match(data)
        if match:
            inner = rewrite_html_links(
                match.group("body"), self.tracking_token, self.unsubscribe_token, self.unsubscribe_template
            )
            self.parts.append(f"<!--{match.group('open')}{inner}{match.group('close')}-->")
            return
        if _DOWNLEVEL_REVEALED_RE.match(data):
            # Python 3.14 reports <![if !mso]> / <![endif]> as bogus comments; keep them as written.
            self.parts.append(f"<!{data}>")
            return
        self.parts.append(f"<!--{data}-->")

    def unknown_decl(self, data):
        # Downlevel-revealed conditionals such as <![if !mso]> / <![endif]>.
        self.parts.append(f"<![{data}]>")

    def handle_decl(self, decl):
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.parts.append(f"<?{data}>")

    def _format_tag(self, tag, attrs, *, closed):
        rewritten_attrs = []
        relay_owned_link = False
        for name, value in attrs:
            if tag.lower() == "a" and name.lower() == "href" and value and _UNIQID in value:
                if self.unsubscribe_token and _MAILCHIMP_ACCOUNT_LINK_RE.match(value):
                    # Relay's unsubscribe page is the single unsubscribe/preferences surface; skip click tracking.
                    rewritten_attrs.append((name, unsubscribe_url(self.unsubscribe_token, self.unsubscribe_template)))
                    relay_owned_link = True
                    continue
                value = value.replace("?e=[UNIQID]", "").replace("&e=[UNIQID]", "")
            if tag.lower() == "a" and name.lower() == "href" and _is_trackable_url(value):
                value = click_redirect_url(self.tracking_token, value)
                relay_owned_link = True
            rewritten_attrs.append((name, value))

        if relay_owned_link and not any(name.lower() == _SES_NO_TRACK for name, _ in rewritten_attrs):
            rewritten_attrs.insert(0, (_SES_NO_TRACK, None))

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
