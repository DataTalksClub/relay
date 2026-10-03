"""Pure campaign body rendering and action-link rewriting."""

from html import escape
from html.parser import HTMLParser
from urllib.parse import urlparse

from mailing.services.public_urls import click_redirect_url, open_pixel_url, unsubscribe_url


def build_campaign_html_body(html_body, tracking_token, unsubscribe_token):
    body = rewrite_html_links(html_body or "", tracking_token)
    pixel = f'<img src="{escape(open_pixel_url(tracking_token), quote=True)}" width="1" height="1" alt="" />'
    unsubscribe_href = escape(unsubscribe_url(unsubscribe_token), quote=True)
    footer = f'<p><a href="{unsubscribe_href}">Unsubscribe or manage preferences</a></p>'
    return f"{body}\n{footer}\n{pixel}"


def build_campaign_text_body(text_body, unsubscribe_token):
    url = unsubscribe_url(unsubscribe_token)
    body = (text_body or "").rstrip()
    if body:
        return f"{body}\n\nUnsubscribe or manage preferences: {url}"
    return f"Unsubscribe or manage preferences: {url}"


def rewrite_html_links(html_body, tracking_token):
    rewriter = _ClickTrackingHTMLRewriter(tracking_token)
    rewriter.feed(html_body)
    rewriter.close()
    return rewriter.output


class _ClickTrackingHTMLRewriter(HTMLParser):
    def __init__(self, tracking_token):
        super().__init__(convert_charrefs=False)
        self.tracking_token = tracking_token
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
