"""Markdown rendering for transactional email bodies.

Ports the AI Shipping Labs email markdown pipeline so imported templates
render exactly like they did in the donor site:

1. The markdown body is a Django template first -- donor templates use
   ``{% if %}`` blocks and ``{{ name }}`` placeholders, so context
   substitution must happen before the markdown parse.
2. The substituted markdown converts to HTML with the same extension set
   AISL email uses: core extensions plus external-link rewriting, with
   mermaid and codehilite off (inboxes run no JS and ship no stylesheet).
3. The HTML is wrapped in the shared email shell (``email_base.html``)
   with a header and footer around the body.
"""

import re
from urllib.parse import urlparse

import markdown as markdown_lib
from django.conf import settings
from django.template.loader import render_to_string
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor

# Matches a line of the form ``<lead>: - <item1> - <item2>[ - <itemN>]`` so it
# can be rewritten into a real markdown list. The mandatory spaces around the
# hyphen keep hyphenated words and em/en dashes intact.
INLINE_BULLET_RE = re.compile(r"^(?P<lead>.*?:) - (?P<rest>.+)$")

# AISL renders email markdown with mermaid and codehilite disabled: inboxes
# cannot run the mermaid runtime and ship no codehilite stylesheet.
MARKDOWN_EMAIL_EXTENSIONS = [
    "fenced_code",
    "tables",
    "attr_list",
    "md_in_html",
]


def normalize_inline_bullets(text):
    """Rewrite inline dash-run lists into real markdown list items.

    Render-time only pass; the stored markdown source is never mutated.
    """
    if not text or " - " not in text:
        return text

    out = []
    for line in text.split("\n"):
        match = INLINE_BULLET_RE.match(line)
        if match:
            items = [item.strip() for item in match.group("rest").split(" - ")]
            if len(items) >= 2 and all(items):
                if out and out[-1].strip():
                    out.append("")
                out.append(match.group("lead"))
                out.append("")
                out.extend(f"- {item}" for item in items)
                out.append("")
                continue
        out.append(line)
    return "\n".join(out)


def email_site_hosts():
    """Lowercase hostnames treated as internal by the link rewriter.

    Source of truth is ``RELAY_EMAIL_SITE_BASE_URL``; empty means every
    absolute ``http(s)://...`` URL is treated as external, the safe default.
    """
    hosts = set()
    site_url = getattr(settings, "RELAY_EMAIL_SITE_BASE_URL", "") or ""
    if site_url:
        netloc = urlparse(site_url).netloc.lower()
        if netloc:
            hosts.add(netloc)
            if netloc.startswith("www."):
                hosts.add(netloc[4:])
            else:
                hosts.add(f"www.{netloc}")
    return hosts


class ExternalLinksTreeprocessor(Treeprocessor):
    """Add ``target="_blank"`` and ``rel="noopener"`` to external links."""

    def run(self, root):
        site_hosts = email_site_hosts()

        for element in root.iter("a"):
            href = (element.get("href") or "").strip()
            if not href:
                continue
            parsed = urlparse(href)
            if (parsed.scheme or "").lower() not in ("http", "https"):
                continue
            if not parsed.netloc or parsed.netloc.lower() in site_hosts:
                continue
            if not element.get("target"):
                element.set("target", "_blank")
            rel_tokens = (element.get("rel", "") or "").split()
            if "noopener" not in {token.lower() for token in rel_tokens}:
                rel_tokens.append("noopener")
            element.set("rel", " ".join(rel_tokens))


class ExternalLinksExtension(Extension):
    def extendMarkdown(self, md):
        md.treeprocessors.register(ExternalLinksTreeprocessor(md), "external_links", 0)


def render_email_markdown(text):
    """Convert email markdown to HTML with the donor extension set."""
    return markdown_lib.markdown(
        normalize_inline_bullets(text or ""),
        extensions=[ExternalLinksExtension(), *MARKDOWN_EMAIL_EXTENSIONS],
        output_format="html",
    )


def wrap_email_html(subject, body_html, *, footer_note=""):
    """Wrap rendered body HTML in the shared email shell with header/footer."""
    return render_to_string(
        "mailing/email_base.html",
        {
            "subject": subject,
            "body_html": body_html,
            "brand_name": getattr(settings, "RELAY_EMAIL_BRAND_NAME", "Relay"),
            "footer_note": footer_note,
        },
    )
