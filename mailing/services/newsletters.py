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

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.text import slugify

from mailing.dry_run import DryRunSuppressed
from mailing.models import (
    Audience,
    Campaign,
    CampaignStatus,
    NewsletterBlock,
    NewsletterIssue,
    NewsletterTestSend,
)
from mailing.services import newsletter_layout as layout
from mailing.services.api_errors import ApiValidationError

logger = logging.getLogger("mailing.newsletters")

MAX_TEST_RECIPIENTS = 10


REFILLABLE_SOURCE_PREFIXES = ("website:", "substack:")


def test_presets():
    """Addresses offered as one-click test recipients in the operator UI."""
    raw = os.environ.get("RELAY_NEWSLETTER_TEST_PRESETS", "alexey@datatalks.club")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


TEST_SEND_TIMEOUT_SECONDS = 15
TEST_SEND_DEADLINE_SECONDS = 40


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


def validate_block(block_type, data, *, draft=False):
    """Return cleaned ``data`` for ``block_type`` or raise ApiValidationError.

    ``draft=True`` skips the required-field check (used for template
    placeholders and blocks still flagged ``needs_review``); every other check
    (types, lengths, URLs, unknown fields) still applies.
    """
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
        if not draft and name in spec["required"] and not value and not spec["defaults"].get(name):
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
    # Only the edited fields: a stale instance must not overwrite e.g. the campaign link.
    issue.save(update_fields=[*fields, "updated_by", "updated_at"])
    return issue


def upsert_issue(client, key, data, *, actor=""):
    key = normalize_issue_key(key)
    issue = NewsletterIssue.objects.filter(client=client, key=key).first()
    if issue is None:
        return create_issue(client, {**data, "key": key}, actor=actor), True
    return update_issue(issue, data, actor=actor), False


@transaction.atomic
def add_block(issue, block_type, data, *, position=None, actor="", needs_review=False, source="", source_note=""):
    """Insert a block. ``needs_review=True`` stores draft data (required fields may be empty)."""
    cleaned = validate_block(block_type, data, draft=bool(needs_review))
    blocks = list(issue.blocks.select_for_update().order_by("position", "id"))
    block = NewsletterBlock(issue=issue, block_type=block_type, data=cleaned, updated_by=actor,
                            needs_review=bool(needs_review), source=_clip(source, 60),
                            source_note=_clip(source_note, 255))
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
    """Strictly validate and save; a successful save means the editor reviewed it."""
    block_type = block_type or block.block_type
    block.data = validate_block(block_type, data)
    block.block_type = block_type
    block.needs_review = False
    if not (block.source or "").startswith(REFILLABLE_SOURCE_PREFIXES):
        # A placeholder's "what to write here" hint is done once the editor saved real content.
        block.source_note = ""
    block.updated_by = actor
    block.save()
    if isinstance(position, int):
        move_block_to(block, position)
    _touch(block.issue, actor)
    return block


@transaction.atomic
def duplicate_block(block, *, actor=""):
    """Insert a copy of ``block`` right after it (same data and review flags); returns the copy."""
    blocks = list(block.issue.blocks.select_for_update().order_by("position", "id"))
    index = next((i for i, item in enumerate(blocks) if item.pk == block.pk), len(blocks) - 1)
    copy = NewsletterBlock(issue=block.issue, block_type=block.block_type, data=dict(block.data or {}),
                           updated_by=actor, needs_review=block.needs_review, source=block.source,
                           source_note=block.source_note, position=index + 1)
    copy.save()
    blocks.insert(index + 1, copy)
    _renumber(blocks)
    _touch(block.issue, actor)
    return copy


@transaction.atomic
def change_block_type(block, new_type, *, actor=""):
    """Switch ``block`` to ``new_type`` keeping shared field values; flags it for review."""
    if new_type not in BLOCK_TYPES:
        raise ApiValidationError({"type": "unknown_block_type"})
    old_spec = BLOCK_TYPES.get(block.block_type, {"defaults": {}})
    new_spec = BLOCK_TYPES[new_type]
    data = {}
    for name in new_spec["fields"]:
        value = (block.data or {}).get(name, "")
        # A value that only repeats the old type's default (e.g. kicker "SPONSORED")
        # is replaced by the new type's default.
        if not value or value == old_spec["defaults"].get(name):
            value = new_spec["defaults"].get(name, "")
        if value:
            data[name] = value
    block.data = validate_block(new_type, data, draft=True)
    block.block_type = new_type
    block.needs_review = True
    block.updated_by = actor
    block.save()
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


def _clip(value, limit):
    return str(value or "")[:limit]


# --------------------------------------------------------------------------- problems

_PROBLEM_TEXT = {
    "required": "{label} is required",
    "invalid_url": "{label} needs a full http(s):// link",
    "too_long": "{label} is too long",
    "unknown_field": "{label} is not a field of this block",
    "must_be_string": "{label} must be text",
}
_MERGE_TAG_RE = re.compile(r"\{\{|\}\}|\*\|[^|]*\|\*")


def _short_label(name):
    return FIELD_LABELS.get(name, name).split(" (")[0]


def _problem_text(field, code):
    parts = field.removeprefix("data.").split(".")
    label = _short_label(parts[0])
    if len(parts) > 1 and parts[1].isdigit():
        return f"{label}: line {int(parts[1]) + 1} needs a full http(s):// link"
    template = _PROBLEM_TEXT.get(code if isinstance(code, str) else "", "{label} is not valid")
    return template.format(label=label)


def block_label(block_type):
    return BLOCK_TYPES.get(block_type, {}).get("label", block_type)


def block_problems(block):
    """Short human-readable reasons why ``block`` is not ready to go out ([] when fine)."""
    if block.block_type not in BLOCK_TYPES:
        return [f"Unknown block type “{block.block_type}”"]
    problems = []
    if block.needs_review:
        problems.append("Needs your review")
    data = block.data if isinstance(block.data, dict) else {}
    try:
        validate_block(block.block_type, data)
    except ApiValidationError as exc:
        problems.extend(_problem_text(field, code) for field, code in exc.errors.items())
    if any(isinstance(value, str) and _MERGE_TAG_RE.search(value) for value in data.values()):
        problems.append("Remove {{ }} or *| |* — merge tags do not work in newsletters")
    return problems


def issue_problems(issue):
    """Every block with problems, plus an issue-level entry when the issue has no blocks."""
    blocks = list(issue.blocks.order_by("position", "id"))
    if not blocks:
        return [{"block_id": None, "position": None, "label": "Issue", "problems": ["The issue has no blocks yet"]}]
    result = []
    for block in blocks:
        problems = block_problems(block)
        if problems:
            result.append({"block_id": block.pk, "position": block.position, "label": block_label(block.block_type),
                           "problems": problems})
    return result


# --------------------------------------------------------------------------- rendering

def _lines(value):
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def _value(spec, data, name):
    return data.get(name) or spec["defaults"].get(name, "")


def _block_rows(block_type, data):
    spec = BLOCK_TYPES.get(block_type)
    if spec is None:
        return ""
    data = data if isinstance(data, dict) else {}
    return layout.block_rows(block_type, spec, lambda name: _value(spec, data, name), _valid_url)


def render_block_html(block_type, data):
    """Mailchimp rows (the #302 layout) for one block; tolerates partial (draft) data. Unknown types render ""."""
    return _block_rows(block_type, data)


def render_block_preview(block_type, data):
    """One block as a standalone HTML document styled like the newsletter ("" when empty)."""
    return layout.block_preview_document(block_type, _block_rows(block_type, data))


def _text_markdown(value):
    """Markdown -> the plain-text style of #302's text part: ``text (url)`` links, ``*`` bullets."""
    text = re.sub(r"\[([^\]]*)\]\(([^)\s]*)\)", r"\1 (\2)", value or "")
    text = re.sub(r"(?m)^\s*[-+]\s+", "* ", text)
    return text.replace("**", "").strip()


def render_block_text(block_type, data):
    spec = BLOCK_TYPES.get(block_type)
    if spec is None:
        return ""
    data = data if isinstance(data, dict) else {}
    get = lambda name: _value(spec, data, name)  # noqa: E731
    render = spec["render"]
    if render == "html":
        return re.sub(r"<[^>]+>", "", get("html")).strip()
    if render == "intro":
        return "\n\n".join(part for part in (get("issue_label"), _text_markdown(get("summary"))) if part)
    parts = []
    if get("kicker"):
        parts.append(get("kicker"))
    if get("title"):
        parts.append(f"\n** {get('title')}\n" + "-" * 60)
    if render == "events":
        for line in _lines(get("events")):
            fields = [field.strip() for field in line.split("|")]
            parts.append(fields[0])
            if len(fields) > 1:
                link = f" ({fields[2]})" if len(fields) > 2 and fields[2] else ""
                host = f" {fields[3]}" if len(fields) > 3 and fields[3] else ""
                parts.append(f"{fields[1]}{link}{host}")
        parts.append(_text_markdown(get("footer")))
    elif render == "links":
        for line in _lines(get("links")):
            fields = [field.strip() for field in line.split("|")]
            link = f" ({fields[1]})" if len(fields) > 1 and fields[1] else ""
            note = f" — {fields[2]}" if len(fields) > 2 and fields[2] else ""
            parts.append(f"* {fields[0]}{link}{note}")
    else:
        if get("meta"):
            parts.append(get("meta"))
        if get("image_link"):
            parts.append(get("image_link"))
        parts.append(_text_markdown(get("body")))
        parts.append("\n".join(f"* {item}" for item in _lines(get("bullets"))))
        parts.append(_text_markdown(get("closing")))
        if get("cta_text") and get("cta_url"):
            parts.append(f"{get('cta_text')} ({get('cta_url')})")
    return "\n\n".join(part for part in parts if part)


def unfinished_note(block):
    """Visible yellow box placed before a ``needs_review`` block in editor previews."""
    note = f" — {block.source_note}" if block.source_note else ""
    return ('<div style="background:#fff4ce;border:2px dashed #e0b400;border-radius:6px;padding:10px 14px;'
            'font-size:14px;line-height:1.4;color:#5c4400;font-family:Arial,sans-serif">'
            f'<strong>Needs content:</strong> {escape(block_label(block.block_type))}{escape(note)}</div>')


TEST_BANNER = ('<div style="background:#fff4ce;border:1px solid #e0b400;border-radius:6px;padding:10px 14px;'
               'margin:0 0 4px;font-size:14px;line-height:1.4;color:#5c4400;text-align:center">TEST EMAIL — links '
               'are not tracked and the unsubscribe link is disabled.</div>')


def _intro_summary(blocks):
    for item in blocks:
        if item.block_type == "intro":
            return (item.data or {}).get("summary", "")
    return ""


def effective_preview_text(issue):
    """The inbox preview: the issue's preview text, else the intro text (what #302 used)."""
    if issue.preview_text:
        return issue.preview_text
    return _plain(_intro_summary(list(issue.blocks.order_by("position", "id"))))[:255]


def _plain(markdown_text):
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", markdown_text or "")
    return re.sub(r"\s+", " ", re.sub(r"[*_`#>]", "", text)).strip()


def render_issue(issue, *, block=None, test=False, mark_unfinished=False):
    """Render the whole issue (or one ``block``) into ``{subject, html_body, text_body}``.

    The HTML is the issue #302 Mailchimp layout (``newsletter_layout``) filled
    from the blocks: the intro goes under the logo, every other block becomes a
    section. Blocks that render to nothing are skipped. ``mark_unfinished=True``
    (editor previews only) puts a visible "Needs content" note before every
    block that still ``needs_review``. The inbox preview falls back to the
    intro text, as in #302, when the issue has no preview text.
    """
    all_blocks = list(issue.blocks.order_by("position", "id"))
    blocks = [block] if block is not None else all_blocks
    intro_rows, items, texts = [], [], []
    for item in blocks:
        if item.block_type not in BLOCK_TYPES:
            continue
        rows = _block_rows(item.block_type, item.data or {})
        if mark_unfinished and item.needs_review:
            rows = layout.note_row(unfinished_note(item)) + rows
        if not rows.strip():
            continue
        if item.block_type == "intro":
            intro_rows.append(rows)
        else:
            items.append((item.block_type, rows))
        text = render_block_text(item.block_type, item.data or {})
        if text.strip():
            texts.append(text)
    subject = issue.subject
    if block is not None:
        subject = f"{subject} — block: {BLOCK_TYPES.get(block.block_type, {}).get('label', block.block_type)}"
    if test:
        subject = f"[TEST] {subject}"
    preheader = issue.preview_text or _plain(_intro_summary(all_blocks))[:255]
    html = layout.document(title=subject, preheader=preheader, banner_html=TEST_BANNER if test else "",
                           intro_html="".join(intro_rows), sections=layout.sections_html(items))
    text = "\n\n".join(["https://datatalks.club/", *texts, FOOTER_TEXT])
    if test:
        # Belt and braces: SES leaves links marked ses:no-track alone even if a
        # default configuration set with click tracking applies.
        html = re.sub(r"<a(?=\s)", "<a ses:no-track", html)
        text = "[TEST EMAIL - links are not tracked; unsubscribe is disabled]\n\n" + text
    return {"subject": subject, "html_body": html, "text_body": text}


# The footer of #302's text part, unchanged.
FOOTER_TEXT = (
    "DataTalks.Club is a global community of data enthusiasts. If you haven't had a chance to join our Slack yet, "
    "use this invite link (http://join.datatalks.club/) . If you have problems with the invite link, fill in this "
    "form (https://airtable.com/shrhUBN51Jy10fjJq) , and we'll add you to Slack manually.\n\n"
    "Do you have any questions? Hit reply, and we'll be glad to answer.\n\nSee you in Slack!\n\n"
    "Subscribe (https://datatalks.club/) · Past issues (https://datatalks.club/blog/) · Sponsor this newsletter "
    "(https://datatalks.club/blog/sponsor-datatalks-club.html)\n\n"
    "https://twitter.com/datatalksclub\nhttps://linkedin.com/company/datatalks-club\n"
    "https://www.youtube.com/c/DataTalksClub\nhttps://datatalks.club/\nmailto:alexey@datatalks.club\n\n"
    "Copyright (C) 2026 DataTalks. All rights reserved.\n\nOur mailing address is:\n"
    "DataTalks, Langhansstr. 70, Berlin, 13086, DE\n\nWant to change how you receive these emails?\n"
)


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
    from mailing.ses_rate_limiter import TRANSACTIONAL  # noqa: PLC0415

    # configuration_set="" sends outside the production configuration set, whose
    # SES open/click tracking would otherwise wrap links in a [TEST] email.
    return send_email(ses_client=ses_client(), source=source, to_email=email, subject=rendered["subject"],
                      html_body=rendered["html_body"], text_body=rendered["text_body"], configuration_set="",
                      kind=TRANSACTIONAL)


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
            "label": block_label(block.block_type),
            "data": block.data, "needs_review": block.needs_review, "source": block.source,
            "source_note": block.source_note, "problems": block_problems(block),
            "updated_by": block.updated_by, "updated_at": block.updated_at.isoformat()}


def campaign_link_payload(campaign):
    if campaign is None:
        return None
    return {"id": campaign.pk, "external_key": campaign.external_key, "status": campaign.status}


def issue_payload(issue, *, with_blocks=True):
    payload = {
        "key": issue.key, "subject": issue.subject, "preview_text": issue.preview_text,
        "issue_number": issue.issue_number,
        "publication_date": issue.publication_date.isoformat() if issue.publication_date else None,
        "template": issue.template_key, "campaign": campaign_link_payload(issue.campaign),
        "problems": len(issue_problems(issue)),
        "updated_by": issue.updated_by, "created_at": issue.created_at.isoformat(),
        "updated_at": issue.updated_at.isoformat(),
    }
    if with_blocks:
        payload["blocks"] = [block_payload(block) for block in issue.blocks.order_by("position", "id")]
    return payload


# --------------------------------------------------------------------------- campaign assembly

NEWSLETTER_METADATA_KEY = "newsletter"


def _resolve_audience(client, audience):
    if isinstance(audience, Audience):
        return audience if audience.organization_id == client.organization_id else None
    if isinstance(audience, str) and audience.strip():
        return Audience.objects.filter(organization_id=client.organization_id, slug=audience.strip()).first()
    if isinstance(audience, int) and not isinstance(audience, bool):
        return Audience.objects.filter(organization_id=client.organization_id, pk=audience).first()
    return None


@transaction.atomic
def assemble_campaign(issue, *, audience, actor=""):
    """Turn a finished issue into a *draft* campaign; returns ``(campaign, created)``.

    ``audience`` is an Audience, its slug or its id, and must belong to the
    client's organization. Re-assembling updates the linked draft. This never
    queues, schedules or sends anything: the campaign stays ``draft`` and goes
    through the normal campaign review/queue flow.
    """
    from mailing.services.api import validate_campaign_external_key  # noqa: PLC0415 - avoid import cycle
    from mailing.services.campaign_composer import EDITOR_METADATA_KEY  # noqa: PLC0415

    original, issue = issue, NewsletterIssue.objects.select_for_update().get(pk=issue.pk)
    client = issue.client
    target = _resolve_audience(client, audience)
    if target is None:
        raise ApiValidationError({"audience": "invalid"})
    if issue_problems(issue):
        raise ApiValidationError({"issue": "has_problems"})
    rendered = render_issue(issue)
    stamp = {"issue_id": issue.pk, "issue_key": issue.key, "assembled_at": timezone.now().isoformat(),
             "assembled_by": actor}
    fields = {"subject": issue.subject, "preview_text": effective_preview_text(issue),
              "html_body": rendered["html_body"], "text_body": rendered["text_body"]}

    campaign = Campaign.objects.select_for_update().filter(pk=issue.campaign_id).first() if issue.campaign_id else None
    if campaign is not None:
        if campaign.status != CampaignStatus.DRAFT:
            raise ApiValidationError({"campaign": "not_draft"}, status_code=409)
        if campaign.audience_id != target.pk:
            # Tag filters are audience-scoped; they cannot carry over to another audience.
            campaign.include_tags, campaign.exclude_tags = [], []
        for name, value in fields.items():
            setattr(campaign, name, value)
        campaign.audience = target
        metadata = dict(campaign.metadata) if isinstance(campaign.metadata, dict) else {}
        metadata.pop(EDITOR_METADATA_KEY, None)
        metadata[NEWSLETTER_METADATA_KEY] = stamp
        campaign.metadata = metadata
        campaign.save()
        created = False
    else:
        external_key = validate_campaign_external_key(issue.key)
        if Campaign.objects.filter(client=client, external_key=external_key).exists():
            raise ApiValidationError({"campaign": "key_in_use"}, status_code=409)
        campaign = Campaign.objects.create(client=client, audience=target, external_key=external_key,
                                           status=CampaignStatus.DRAFT,
                                           metadata={NEWSLETTER_METADATA_KEY: stamp}, **fields)
        created = True
    issue.campaign = campaign
    issue.updated_by = actor
    issue.save(update_fields=["campaign", "updated_by", "updated_at"])
    original.campaign = campaign
    logger.info("newsletter issue assembled client=%s issue=%s campaign=%s created=%s by=%s",
                client.slug, issue.key, campaign.pk, created, actor)
    return campaign, created


def campaign_newsletter_issue(campaign):
    """The newsletter issue a campaign was assembled from, or None."""
    if campaign is None or not campaign.pk:
        return None
    return NewsletterIssue.objects.filter(campaign=campaign).order_by("-updated_at", "-id").first()


# Templates and auto-fill live in their own module; re-exported so callers use ``svc.<name>``.
from mailing.services.newsletter_templates import (  # noqa: E402,F401
    NEWSLETTER_TEMPLATES,
    create_issue_from_template,
    issue_defaults,
    newsletter_templates,
    refill_block,
)

