"""Newsletter issues built from typed blocks: validation, rendering, test sends.

One implementation serves the operator UI (Alexey, Valeriia) and the client
API (DataOps/Orin), so both paths validate and render identically.

Block types follow the archive catalog
(agent-reports/2026-11-09-newsletter-blocks-catalog.md): most are "cards"
(kicker, title, image, body, CTA) with different defaults; events, link lists,
the intro and a raw-HTML escape hatch render differently.

Test sends are deliberately not campaigns: the subject is prefixed with
``[TEST]``, links are not rewritten for tracking, no open pixel is added and no
unsubscribe token for a real contact is minted.
"""

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from html import escape
from urllib.parse import urlsplit

import markdown as markdown_lib
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Max
from django.utils.dateparse import parse_date
from django.utils.text import slugify

from mailing.dry_run import DryRunSuppressed
from mailing.models import NewsletterBlock, NewsletterIssue, NewsletterTestSend
from mailing.services.api_errors import ApiValidationError

logger = logging.getLogger("mailing.newsletters")

MAX_TEST_RECIPIENTS = 10


def test_presets():
    """Addresses offered as one-click test recipients in the operator UI."""
    raw = os.environ.get("RELAY_NEWSLETTER_TEST_PRESETS", "alexey@datatalks.club")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


TEST_SEND_TIMEOUT_SECONDS = 15
TEST_SEND_DEADLINE_SECONDS = 40

BRAND_BLUE = "#2056A2"
FONT = "'Helvetica Neue',Helvetica,Arial,Verdana,sans-serif"
LOGO_URL = ("https://mcusercontent.com/0d7822ab98152f5afc118c176/images/"
            "bf262d0f-68be-b981-422b-e8e5cfc8eeca.png")

FIELD_LABELS = {
    "kicker": "Section label",
    "title": "Title",
    "meta": "Date / details line",
    "image_url": "Image URL",
    "image_link": "Image link",
    "body": "Text (Markdown: **bold**, [link](https://...), lists)",
    "bullets": "Bullet points (one per line)",
    "closing": "Closing text (Markdown)",
    "cta_text": "Button text",
    "cta_url": "Button link",
    "issue_label": "Issue label (e.g. #303 · November 16, 2026)",
    "summary": "This week (Markdown)",
    "events": "Events (one per line: when · type | title | https://link | with Speaker)",
    "links": "Links (one per line: title | https://link | optional note)",
    "footer": "Text under the list (Markdown)",
    "html": "HTML",
}
FIELD_KINDS = {
    "kicker": "text", "title": "text", "meta": "text", "image_url": "url", "image_link": "url",
    "body": "markdown", "bullets": "lines", "closing": "markdown", "cta_text": "text", "cta_url": "url",
    "issue_label": "text", "summary": "markdown", "events": "lines", "links": "lines", "footer": "markdown",
    "html": "html",
}
CARD_FIELDS = ["kicker", "title", "meta", "image_url", "image_link", "body", "bullets", "closing", "cta_text", "cta_url"]


def _card(label, kicker="", required=("title",)):
    return {"label": label, "render": "card", "fields": CARD_FIELDS, "defaults": {"kicker": kicker},
            "required": list(required)}


BLOCK_TYPES = {
    "intro": {"label": "Intro (issue number, date, this week)", "render": "intro",
              "fields": ["issue_label", "summary"], "defaults": {}, "required": ["summary"]},
    "sponsor": _card("Sponsor (main)", "SPONSORED", ("title", "cta_text", "cta_url")),
    "sponsor_secondary": _card("Sponsor (secondary)", "SPONSORED", ("title", "cta_url")),
    "course_promo": _card("Course promo (when no sponsor)", "", ("title", "cta_text", "cta_url")),
    "own_promo": _card("Own promo (when no sponsor)", "", ("title",)),
    "course_update": _card("Course update / preparation card", ""),
    "event_spotlight": _card("Event spotlight", "EVENT", ("title", "cta_url")),
    "events_list": {"label": "Upcoming events list", "render": "events",
                    "fields": ["kicker", "title", "events", "footer"],
                    "defaults": {"kicker": "EVENTS", "title": "Upcoming Events"}, "required": ["events"]},
    "recording": _card("Recording (workshop / webinar)", "RECORDINGS"),
    "podcast_episode": _card("Podcast episode", "PODCAST"),
    "alexey_article": _card("From Alexey on Data (article)", "READ"),
    "book_of_week": _card("Book of the week", "BOOK OF THE WEEK"),
    "feature": _card("Feature (open-source spotlight, project, survey)", ""),
    "link_list": {"label": "Link list (articles, digest)", "render": "links",
                  "fields": ["kicker", "title", "links"], "defaults": {}, "required": ["links"]},
    "html": {"label": "Custom HTML", "render": "html", "fields": ["html"], "defaults": {}, "required": ["html"]},
}

MAX_LENGTHS = {"text": 500, "url": 2000, "markdown": 20_000, "lines": 20_000, "html": 100_000}


# --------------------------------------------------------------------------- validation

def _valid_url(value):
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username
            and not parsed.password and not any(ord(c) < 33 for c in value))


def block_type_payload(key):
    spec = BLOCK_TYPES[key]
    return {
        "type": key,
        "label": spec["label"],
        "fields": [{"name": name, "label": FIELD_LABELS[name], "kind": FIELD_KINDS[name],
                    "required": name in spec["required"], "default": spec["defaults"].get(name, "")}
                   for name in spec["fields"]],
    }


def block_types_payload():
    return {"block_types": [block_type_payload(key) for key in BLOCK_TYPES]}


def validate_block(block_type, data):
    """Return cleaned ``data`` for ``block_type`` or raise ApiValidationError."""
    if block_type not in BLOCK_TYPES:
        raise ApiValidationError({"type": "unknown_block_type"})
    if not isinstance(data, dict):
        raise ApiValidationError({"data": "must_be_object"})
    spec = BLOCK_TYPES[block_type]
    unknown = sorted(set(data) - set(spec["fields"]))
    errors = {f"data.{name}": "unknown_field" for name in unknown}
    cleaned = {}
    for name in spec["fields"]:
        kind = FIELD_KINDS[name]
        value = data.get(name, "")
        if value is None:
            value = ""
        if kind == "lines" and isinstance(value, list):
            value = "\n".join(str(item) for item in value)
        if not isinstance(value, str):
            errors[f"data.{name}"] = "must_be_string"
            continue
        value = value.strip() if kind != "html" else value
        if len(value) > MAX_LENGTHS[kind]:
            errors[f"data.{name}"] = "too_long"
            continue
        if kind == "url" and value and not _valid_url(value):
            errors[f"data.{name}"] = "invalid_url"
            continue
        if name in spec["required"] and not value and not spec["defaults"].get(name):
            errors[f"data.{name}"] = "required"
            continue
        if name == "events":
            for index, line in enumerate(_lines(value)):
                parts = [part.strip() for part in line.split("|")]
                if len(parts) > 2 and parts[2] and not _valid_url(parts[2]):
                    errors[f"data.events.{index}"] = "invalid_url"
        if name == "links":
            for index, line in enumerate(_lines(value)):
                parts = [part.strip() for part in line.split("|")]
                if len(parts) < 2 or not _valid_url(parts[1]):
                    errors[f"data.links.{index}"] = "invalid_url"
        if value:
            cleaned[name] = value
    if errors:
        raise ApiValidationError(errors)
    return cleaned


def validate_issue_fields(data, *, partial=False):
    errors, cleaned = {}, {}
    if "subject" in data or not partial:
        subject = data.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            errors["subject"] = "required"
        elif len(subject.strip()) > 255:
            errors["subject"] = "too_long"
        else:
            cleaned["subject"] = subject.strip()
    if "preview_text" in data:
        value = data.get("preview_text") or ""
        if not isinstance(value, str):
            errors["preview_text"] = "must_be_string"
        elif len(value.strip()) > 255:
            errors["preview_text"] = "too_long"
        else:
            cleaned["preview_text"] = value.strip()
    if "issue_number" in data:
        value = data.get("issue_number")
        if value in (None, ""):
            cleaned["issue_number"] = None
        else:
            try:
                number = int(value)
            except (TypeError, ValueError):
                number = -1
            if number < 0 or isinstance(value, bool):
                errors["issue_number"] = "invalid"
            else:
                cleaned["issue_number"] = number
    if "publication_date" in data:
        value = data.get("publication_date")
        if value in (None, ""):
            cleaned["publication_date"] = None
        else:
            parsed = parse_date(value) if isinstance(value, str) else None
            if parsed is None:
                errors["publication_date"] = "invalid"
            else:
                cleaned["publication_date"] = parsed
    if errors:
        raise ApiValidationError(errors)
    return cleaned


def normalize_issue_key(value):
    if not isinstance(value, str) or not value.strip():
        raise ApiValidationError({"key": "required"})
    key = slugify(value.strip())
    if not key or len(key) > 120:
        raise ApiValidationError({"key": "invalid"})
    return key


def validate_test_emails(value):
    if isinstance(value, str):
        value = [item for item in re.split(r"[,;\s]+", value.strip()) if item]
    if not isinstance(value, list) or not value:
        raise ApiValidationError({"emails": "required"})
    emails, invalid = [], []
    for item in value:
        email = item.strip().lower() if isinstance(item, str) else ""
        try:
            validate_email(email)
        except ValidationError:
            invalid.append(str(item))
            continue
        if email not in emails:
            emails.append(email)
    if invalid:
        raise ApiValidationError({"emails": {"invalid": invalid}})
    if len(emails) > MAX_TEST_RECIPIENTS:
        raise ApiValidationError({"emails": "too_many", "max": MAX_TEST_RECIPIENTS})
    return emails


# --------------------------------------------------------------------------- CRUD (shared by UI and API)

def actor_label(actor=None, api_client=None):
    if actor is not None and getattr(actor, "is_authenticated", False):
        return actor.email or actor.get_username()
    if api_client is not None:
        return f"api:{api_client.slug}"
    return ""


def create_issue(client, data, *, actor=""):
    key = normalize_issue_key(data.get("key") or data.get("subject") or "")
    fields = validate_issue_fields(data)
    if NewsletterIssue.objects.filter(client=client, key=key).exists():
        raise ApiValidationError({"key": "already_exists"}, status_code=409)
    issue = NewsletterIssue.objects.create(client=client, key=key, updated_by=actor, **fields)
    logger.info("newsletter issue created client=%s key=%s by=%s", client.slug, key, actor)
    return issue


def update_issue(issue, data, *, actor=""):
    fields = validate_issue_fields(data, partial=True)
    for name, value in fields.items():
        setattr(issue, name, value)
    issue.updated_by = actor
    issue.save()
    return issue


def upsert_issue(client, key, data, *, actor=""):
    key = normalize_issue_key(key)
    issue = NewsletterIssue.objects.filter(client=client, key=key).first()
    if issue is None:
        return create_issue(client, {**data, "key": key}, actor=actor), True
    return update_issue(issue, data, actor=actor), False


@transaction.atomic
def add_block(issue, block_type, data, *, position=None, actor=""):
    cleaned = validate_block(block_type, data)
    blocks = list(issue.blocks.select_for_update().order_by("position", "id"))
    block = NewsletterBlock(issue=issue, block_type=block_type, data=cleaned, updated_by=actor)
    if position is None or not isinstance(position, int) or position < 0 or position >= len(blocks):
        blocks.append(block)
    else:
        blocks.insert(position, block)
    block.position = blocks.index(block)
    block.save()
    _renumber(blocks)
    _touch(issue, actor)
    return block


@transaction.atomic
def update_block(block, data, *, block_type=None, position=None, actor=""):
    block_type = block_type or block.block_type
    block.data = validate_block(block_type, data)
    block.block_type = block_type
    block.updated_by = actor
    block.save()
    if isinstance(position, int):
        move_block_to(block, position)
    _touch(block.issue, actor)
    return block


@transaction.atomic
def delete_block(block, *, actor=""):
    issue = block.issue
    block.delete()
    _renumber(list(issue.blocks.select_for_update().order_by("position", "id")))
    _touch(issue, actor)


@transaction.atomic
def move_block_to(block, position):
    blocks = [item for item in block.issue.blocks.select_for_update().order_by("position", "id") if item.pk != block.pk]
    position = max(0, min(position, len(blocks)))
    blocks.insert(position, block)
    _renumber(blocks)


def move_block(block, direction):
    move_block_to(block, block.position + (-1 if direction == "up" else 1))


def _renumber(blocks):
    for index, item in enumerate(blocks):
        if item.position != index:
            item.position = index
            NewsletterBlock.objects.filter(pk=item.pk).update(position=index)


def _touch(issue, actor):
    issue.updated_by = actor
    issue.save(update_fields=["updated_by", "updated_at"])


def next_position(issue):
    return (issue.blocks.aggregate(value=Max("position"))["value"] or -1) + 1


# --------------------------------------------------------------------------- rendering

def _lines(value):
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


_SAFE_HREF = re.compile(r'href="(?!https?://|mailto:)[^"]*"')


def render_markdown(text):
    """Render operator Markdown to inline-styled email HTML. Raw HTML is escaped."""
    html = markdown_lib.markdown(escape(text or "", quote=False), extensions=["sane_lists", "nl2br"])
    html = _SAFE_HREF.sub('href="#"', html)
    html = html.replace("<p>", '<p style="margin:0 0 12px;font-size:16px;line-height:1.5;color:#000000">')
    html = html.replace("<ul>", '<ul style="margin:0 0 12px;padding-left:22px;font-size:16px;line-height:1.5">')
    html = html.replace("<ol>", '<ol style="margin:0 0 12px;padding-left:22px;font-size:16px;line-height:1.5">')
    html = html.replace("<li>", '<li style="margin:0 0 6px">')
    html = html.replace("<a ", '<a style="color:#006ab1;text-decoration:underline" ')
    return html


def _kicker(text):
    if not text:
        return ""
    return (f'<p style="margin:0 0 6px;font-size:13px;font-weight:bold;letter-spacing:1.5px;'
            f'text-transform:uppercase;color:{BRAND_BLUE}">{escape(text)}</p>')


def _title(text, url=""):
    if not text:
        return ""
    inner = escape(text)
    if url:
        inner = f'<a href="{escape(url)}" style="color:#000000;text-decoration:none">{inner}</a>'
    return f'<h2 style="margin:0 0 12px;font-size:22px;line-height:1.3;color:#000000">{inner}</h2>'


def _button(text, url):
    if not (text and url):
        return ""
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 4px"><tr>'
            f'<td style="background:{BRAND_BLUE};border-radius:8px">'
            f'<a href="{escape(url)}" style="display:inline-block;padding:14px 28px;font-size:16px;'
            f'font-weight:bold;color:#ffffff;text-decoration:none;border-radius:8px">{escape(text)}</a>'
            f'</td></tr></table>')


def _image(url, link="", alt=""):
    if not url:
        return ""
    img = (f'<img src="{escape(url)}" alt="{escape(alt)}" width="612" '
           f'style="display:block;width:100%;max-width:612px;height:auto;border:0;border-radius:8px;margin:0 0 12px">')
    if link:
        return f'<a href="{escape(link)}">{img}</a>'
    return img


def _value(spec, data, name):
    return data.get(name) or spec["defaults"].get(name, "")


def render_block_html(block_type, data):
    spec = BLOCK_TYPES[block_type]
    get = lambda name: _value(spec, data, name)  # noqa: E731
    render = spec["render"]
    if render == "html":
        return get("html")
    if render == "intro":
        label = get("issue_label")
        head = (f'<p style="margin:0 0 8px;font-size:14px;font-weight:bold;color:{BRAND_BLUE}">{escape(label)}</p>'
                if label else "")
        return head + render_markdown(get("summary"))
    if render == "events":
        items = []
        for line in _lines(get("events")):
            parts = [part.strip() for part in line.split("|")]
            when, title = parts[0], parts[1] if len(parts) > 1 else ""
            url, host = (parts[2] if len(parts) > 2 else ""), (parts[3] if len(parts) > 3 else "")
            title_html = f'<a href="{escape(url)}" style="color:#006ab1">{escape(title)}</a>' if url else escape(title)
            host_html = f" {escape(host)}" if host else ""
            items.append(f'<p style="margin:0 0 14px;font-size:16px;line-height:1.5"><strong>{escape(when)}</strong>'
                         f'{"<br>" + title_html + host_html if title else ""}</p>')
        return _kicker(get("kicker")) + _title(get("title")) + "".join(items) + render_markdown(get("footer"))
    if render == "links":
        items = []
        for line in _lines(get("links")):
            parts = [part.strip() for part in line.split("|")]
            note = f' — {escape(parts[2])}' if len(parts) > 2 and parts[2] else ""
            items.append(f'<li style="margin:0 0 8px"><a href="{escape(parts[1])}" style="color:#006ab1">'
                         f'{escape(parts[0])}</a>{note}</li>')
        return (_kicker(get("kicker")) + _title(get("title"))
                + f'<ul style="margin:0 0 12px;padding-left:22px;font-size:16px;line-height:1.5">{"".join(items)}</ul>')
    bullets = _lines(get("bullets"))
    bullets_html = ("<ul style=\"margin:0 0 12px;padding-left:22px;font-size:16px;line-height:1.5\">"
                    + "".join(f'<li style="margin:0 0 6px">{escape(item)}</li>' for item in bullets) + "</ul>"
                    if bullets else "")
    meta = get("meta")
    meta_html = (f'<p style="margin:0 0 10px;font-size:15px;font-weight:bold;color:#333333">{escape(meta)}</p>'
                 if meta else "")
    return (_kicker(get("kicker")) + _title(get("title"), get("image_link") if not get("image_url") else "")
            + meta_html + _image(get("image_url"), get("image_link"), get("title"))
            + render_markdown(get("body")) + bullets_html + render_markdown(get("closing"))
            + _button(get("cta_text"), get("cta_url")))


def render_block_text(block_type, data):
    spec = BLOCK_TYPES[block_type]
    get = lambda name: _value(spec, data, name)  # noqa: E731
    if spec["render"] == "html":
        return re.sub(r"<[^>]+>", "", get("html")).strip()
    parts = []
    for name in spec["fields"]:
        value = get(name)
        if not value or name in {"image_url", "image_link", "cta_url"}:
            continue
        if name == "cta_text" and get("cta_url"):
            parts.append(f"{value}: {get('cta_url')}")
        elif name in {"events", "links"}:
            parts.extend(" ".join(part.strip() for part in line.split("|")) for line in _lines(value))
        else:
            parts.append(value)
    return "\n".join(parts)


_DIVIDER = '<hr style="border:0;border-top:1px solid #e3e3e3;margin:28px 0">'


def render_issue(issue, *, block=None, test=False):
    """Render the whole issue (or one ``block``) into ``{subject, html_body, text_body}``."""
    blocks = [block] if block is not None else list(issue.blocks.order_by("position", "id"))
    sections, texts = [], []
    for item in blocks:
        if item.block_type not in BLOCK_TYPES:
            continue
        sections.append(render_block_html(item.block_type, item.data or {}))
        texts.append(render_block_text(item.block_type, item.data or {}))
    subject = issue.subject
    if block is not None:
        subject = f"{subject} — block: {BLOCK_TYPES.get(block.block_type, {}).get('label', block.block_type)}"
    if test:
        subject = f"[TEST] {subject}"
    preheader = escape(issue.preview_text)
    test_banner = ('<div style="background:#fff4ce;border:1px solid #e0b400;border-radius:6px;padding:10px 14px;'
                   'margin:0 0 20px;font-size:14px;color:#5c4400">TEST EMAIL — links are not tracked and the '
                   'unsubscribe link is disabled.</div>') if test else ""
    footer = (
        '<p style="margin:0 0 12px;font-size:15px;line-height:1.5;color:#333333">DataTalks.Club is a global community of '
        'data enthusiasts. If you haven\'t had a chance to join our Slack yet, use '
        '<a href="https://join.datatalks.club/" style="color:#006ab1">this invite link</a>. If you have problems with '
        'the invite link, <a href="https://airtable.com/shrhUBN51Jy10fjJq" style="color:#006ab1">fill in this form</a>, '
        'and we\'ll add you to Slack manually.</p>'
        '<p style="margin:0 0 12px;font-size:15px;line-height:1.5;color:#333333">Do you have any questions? Hit reply, '
        'and we\'ll be glad to answer. See you in Slack!</p>'
        '<p style="margin:0;font-size:13px;color:#656565"><a href="https://datatalks.club/" style="color:#656565">'
        'Subscribe</a> · <a href="https://datatalks.club/blog/sponsor-datatalks-club.html" style="color:#656565">'
        'Sponsor this newsletter</a></p>'
    )
    html = (
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,'
        f'initial-scale=1"><title>{escape(subject)}</title></head>'
        f'<body style="margin:0;padding:0;background:#ECECFB;font-family:{FONT}">'
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">{preheader}</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#ECECFB">'
        '<tr><td align="center" style="padding:24px 12px">'
        '<table role="presentation" width="660" cellpadding="0" cellspacing="0" '
        'style="width:100%;max-width:660px;background:#ffffff;border-radius:8px">'
        '<tr><td style="padding:24px 24px 8px">'
        f'<a href="https://datatalks.club/"><img src="{LOGO_URL}" alt="DataTalks.Club Weekly newsletter" width="612" '
        'style="display:block;width:100%;max-width:612px;height:auto;border:0"></a></td></tr>'
        f'<tr><td style="padding:12px 24px 24px;font-family:{FONT}">{test_banner}'
        + _DIVIDER.join(sections)
        + _DIVIDER + footer
        + '</td></tr></table></td></tr></table></body></html>'
    )
    text = "\n\n----\n\n".join(texts)
    if test:
        text = "[TEST EMAIL - links are not tracked; unsubscribe is disabled]\n\n" + text
    return {"subject": subject, "html_body": html, "text_body": text}


# --------------------------------------------------------------------------- test delivery

def _sender_for(client):
    from mailing.services.senders import resolve_sender_email  # noqa: PLC0415 - avoid import cycle

    try:
        return resolve_sender_email(client).email
    except ApiValidationError:
        return settings.DEFAULT_FROM_EMAIL


def _deliver(source, email, rendered):
    from mailing.aws import ses_client  # noqa: PLC0415 - patched in tests
    from mailing.ses import send_email  # noqa: PLC0415

    return send_email(ses_client=ses_client(), source=source, to_email=email, subject=rendered["subject"],
                      html_body=rendered["html_body"], text_body=rendered["text_body"])


def send_test(issue, emails, *, block=None, actor=None, via="ui", deliver=None):
    """Send a [TEST] copy to explicit addresses, synchronously and with a timeout.

    Returns ``{"status": ..., "results": [{email, status, message_id, error}]}``.
    Per-address status: ``sent`` (provider accepted), ``failed`` (provider
    rejected; error shown), ``timeout`` (no answer in time; the email may still
    arrive, do not resend blindly), ``dry_run`` (transport disabled), or
    ``not_attempted`` (skipped after a timeout or the overall deadline).
    Every attempt is logged and stored in ``NewsletterTestSend``.
    """
    if block is None and not issue.blocks.exists():
        raise ApiValidationError({"issue": "no_blocks"})
    rendered = render_issue(issue, block=block, test=True)
    source = _sender_for(issue.client)
    deliver = deliver or _deliver
    results = []
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="newsletter-test")
    deadline = time.monotonic() + TEST_SEND_DEADLINE_SECONDS
    stop = False
    try:
        for email in emails:
            if stop or time.monotonic() >= deadline:
                results.append({"email": email, "status": "not_attempted", "message_id": "", "error": ""})
                continue
            future = executor.submit(deliver, source, email, rendered)
            try:
                message_id = future.result(timeout=min(TEST_SEND_TIMEOUT_SECONDS, deadline - time.monotonic()))
                results.append({"email": email, "status": "sent", "message_id": str(message_id or ""), "error": ""})
            except FutureTimeoutError:
                stop = True
                results.append({"email": email, "status": "timeout", "message_id": "",
                                "error": f"No answer from the email provider within {TEST_SEND_TIMEOUT_SECONDS}s; "
                                         "the email may still arrive."})
            except DryRunSuppressed:
                results.append({"email": email, "status": "dry_run", "message_id": "",
                                "error": "Email sending is disabled on this deployment (dry run)."})
            except ClientError as exc:
                error = exc.response.get("Error", {})
                results.append({"email": email, "status": "failed", "message_id": "",
                                "error": f"{error.get('Code', 'ClientError')}: {error.get('Message', '')}".strip()})
            except (BotoCoreError, Exception) as exc:  # noqa: BLE001 - surfaced to the operator and logged
                results.append({"email": email, "status": "failed", "message_id": "",
                                "error": f"{type(exc).__name__}: {exc}"})
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    user = actor if actor is not None and getattr(actor, "is_authenticated", False) else None
    for item in results:
        log = logger.info if item["status"] == "sent" else logger.warning
        log("newsletter test send issue=%s block=%s via=%s to=%s status=%s message_id=%s error=%s",
            issue.key, getattr(block, "pk", None), via, item["email"], item["status"], item["message_id"],
            item["error"])
        NewsletterTestSend.objects.create(issue=issue, block=block, actor=user, via=via, email=item["email"],
                                          status=item["status"], message_id=item["message_id"],
                                          error=item["error"][:2000])
    statuses = {item["status"] for item in results}
    overall = "sent" if statuses == {"sent"} else ("failed" if "sent" not in statuses else "partial")
    return {"status": overall, "subject": rendered["subject"], "from": source, "results": results}


# --------------------------------------------------------------------------- payloads

def block_payload(block):
    return {"id": block.pk, "position": block.position, "type": block.block_type,
            "label": BLOCK_TYPES.get(block.block_type, {}).get("label", block.block_type),
            "data": block.data, "updated_by": block.updated_by, "updated_at": block.updated_at.isoformat()}


def issue_payload(issue, *, with_blocks=True):
    payload = {
        "key": issue.key, "subject": issue.subject, "preview_text": issue.preview_text,
        "issue_number": issue.issue_number,
        "publication_date": issue.publication_date.isoformat() if issue.publication_date else None,
        "updated_by": issue.updated_by, "created_at": issue.created_at.isoformat(),
        "updated_at": issue.updated_at.isoformat(),
    }
    if with_blocks:
        payload["blocks"] = [block_payload(block) for block in issue.blocks.order_by("position", "id")]
    return payload
