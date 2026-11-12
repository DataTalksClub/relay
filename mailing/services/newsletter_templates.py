"""Newsletter issue templates: starter block structure, suggested values, auto-fill.

``create_issue_from_template`` builds a new issue with the standard
DataTalks.Club Weekly block order (mirroring issue #302). Blocks are filled
from public sources (``newsletter_sources``) where possible and otherwise
created as clearly marked placeholders (``needs_review=True``) for the editor.

Everything here is re-exported from ``mailing.services.newsletters``; callers
use ``svc.<name>``. The core module is imported lazily to avoid an import cycle.
"""

import datetime as dt
from collections import OrderedDict

from django.db import transaction
from django.db.models import Max

from mailing.services import newsletter_sources as sources
from mailing.services.api_errors import ApiValidationError

NEWSLETTER_TEMPLATES = OrderedDict([
    ("dtc-weekly", {
        "label": "DataTalks.Club Weekly",
        "description": ("The standard weekly structure: intro, sponsor, course updates, upcoming events, "
                        "recordings, podcast, Alexey's article and book of the week. Events, recordings, "
                        "the article and the book are filled in from datatalks.club and Substack where possible."),
    }),
    ("blank", {
        "label": "Blank newsletter",
        "description": "An empty issue. Add every block yourself.",
    }),
])

# Issue #302 went out on Monday 2026-11-09; weekly numbering continues from there.
ANCHOR_ISSUE_NUMBER = 302
ANCHOR_DATE = dt.date(2026, 11, 9)

SPONSOR_NOTE = ("Paste the sponsor's copy from the sponsor document (title, image, text, button). "
                "No sponsor this week? Change the block type to “Course promo” or “Own promo”.")
INTRO_NOTE = "Write 1–3 sentences about what is in this issue."
COURSE_NOTE = "Course news for this week (deadlines, what to prepare). Duplicate the block for each course or delete it."
EVENTS_EMPTY_NOTE = "No upcoming events were found on datatalks.club — add them manually or delete this block."
RECORDING_NOTE = "Recording details were filled from datatalks.club events. Add a 2–3 sentence summary."

SOURCE_LABELS = {
    "website:events": "datatalks.club events",
    "website:recordings": "datatalks.club events",
    "website:podcast": "datatalks.club events",
    "website:books": "the datatalks.club books page",
    "substack:alexey": "Alexey's Substack",
}


def _core():
    from mailing.services import newsletters  # noqa: PLC0415 - newsletters re-exports this module

    return newsletters


def _today():
    return dt.datetime.now(sources.EVENT_ZONE).date()


def _note_date(day):
    return f"{day.day} {day:%b %Y}"


def _long_date(day):
    return f"{day:%B} {day.day}, {day.year}"


# Total time all source fetches of one create request may take (gunicorn times out at 60 s).
AUTOFILL_BUDGET_SECONDS = 25


def newsletter_templates():
    return [{"key": key, "label": spec["label"], "description": spec["description"]}
            for key, spec in NEWSLETTER_TEMPLATES.items()]


def _check_template(template_key):
    if template_key not in NEWSLETTER_TEMPLATES:
        raise ApiValidationError({"template": "unknown"})


def next_monday(today):
    return today + dt.timedelta(days=(7 - today.weekday()) % 7 or 7)


def issue_defaults(client, template_key, *, today=None):
    """Suggested form values for a new issue: ``key``, ``subject``, ``preview_text``,
    ``issue_number`` (int or None) and ``publication_date`` (ISO string)."""
    _check_template(template_key)
    publication_date = next_monday(today or _today())
    if template_key == "blank":
        return {"key": "", "subject": "", "preview_text": "", "issue_number": None,
                "publication_date": publication_date.isoformat()}
    highest = client.newsletter_issues.aggregate(value=Max("issue_number"))["value"]
    if highest is not None:
        number = highest + 1
    else:
        number = max(1, ANCHOR_ISSUE_NUMBER + (publication_date - ANCHOR_DATE).days // 7)
    return {"key": f"weekly-{number}", "subject": f"DataTalks.Club Weekly #{number}", "preview_text": "",
            "issue_number": number, "publication_date": publication_date.isoformat()}


# --------------------------------------------------------------------------- block planning

def _needs_input(block_type, data):
    """True when an auto-filled block still lacks something the editor must add."""
    core = _core()
    try:
        core.validate_block(block_type, data)
    except ApiValidationError:
        return True
    if block_type in {"recording", "podcast_episode"}:
        return not data.get("body")
    if block_type in {"alexey_article", "book_of_week"}:
        return not all(data.get(name) for name in ("title", "body", "cta_text", "cta_url"))
    return False


def _auto_item(block_type, data, source, *, today, review_note=None):
    """Plan entry for source data; None when the data does not validate even as a draft."""
    core = _core()
    try:
        cleaned = core.validate_block(block_type, data, draft=True)
    except ApiValidationError as exc:
        core.logger.warning("newsletter autofill data rejected type=%s source=%s errors=%s",
                            block_type, source, exc.errors)
        return None
    needs_review = _needs_input(block_type, cleaned)
    source_note = review_note or (f"Filled automatically from {SOURCE_LABELS[source]} on {_note_date(today)}. "
                                  "Check and edit freely.")
    return {"type": block_type, "data": cleaned, "needs_review": needs_review, "source": source,
            "source_note": source_note, "status": "auto", "note": source_note}


def _placeholder(block_type, note, *, data=None, source="placeholder", report_note=None):
    return {"type": block_type, "data": data or {}, "needs_review": True, "source": source,
            "source_note": note, "status": "placeholder", "note": report_note or note}


def _skipped(block_type, note):
    return {"type": block_type, "status": "skipped", "note": note}


def _plan_dtc_weekly(fields, *, autofill, today):
    publication_date = fields.get("publication_date")
    number = fields.get("issue_number")
    plan = []

    label_parts = [f"#{number}" if number else "", _long_date(publication_date) if publication_date else ""]
    issue_label = " · ".join(part for part in label_parts if part)
    plan.append(_placeholder("intro", INTRO_NOTE, data={"issue_label": issue_label} if issue_label else {},
                             source="computed" if issue_label else "placeholder"))
    plan.append(_placeholder("sponsor", SPONSOR_NOTE))
    plan.append(_placeholder("course_update", COURSE_NOTE))

    active = bool(autofill and publication_date and sources.autofill_enabled())
    if not active:
        reason = ("Autofill is turned off." if publication_date else "No publication date, so nothing was filled in.")
        events_item = _placeholder("events_list", EVENTS_EMPTY_NOTE, source="website:events",
                                   data={"footer": _events_footer()}, report_note=f"{reason} {EVENTS_EMPTY_NOTE}")
        plan.append(events_item)
        for block_type in ("recording", "podcast_episode", "alexey_article", "book_of_week"):
            plan.append(_skipped(block_type, reason))
        return plan

    events = sources.events_list_data(publication_date)
    item = _auto_item("events_list", events, "website:events", today=today) if events else None
    if item is None:
        unreachable = not sources.all_events()
        report_note = ("Could not load events from datatalks.club. " if unreachable else "") + EVENTS_EMPTY_NOTE
        item = _placeholder("events_list", EVENTS_EMPTY_NOTE, source="website:events",
                            data={"footer": _events_footer()}, report_note=report_note)
    plan.append(item)

    window = f"the 7 days before {_long_date(publication_date)}"
    for block_type, source, fetch, missing in (
        ("recording", "website:recordings", sources.recordings_data, f"No workshop or webinar recordings from {window}."),
        ("podcast_episode", "website:podcast", sources.podcast_data, f"No podcast recordings from {window}."),
    ):
        items = [_auto_item(block_type, data, source, today=today, review_note=RECORDING_NOTE)
                 for data in fetch(publication_date)]
        items = [entry for entry in items if entry is not None]
        plan.extend(items or [_skipped(block_type, missing)])

    article = sources.alexey_article_data(publication_date)
    item = _auto_item("alexey_article", article, "substack:alexey", today=today) if article else None
    plan.append(item or _skipped("alexey_article", "No new post on Alexey's Substack in the 8 days before "
                                                   f"{_long_date(publication_date)}."))

    book = sources.book_data(publication_date)
    item = _auto_item("book_of_week", book, "website:books", today=today) if book else None
    plan.append(item or _skipped("book_of_week", "No book of the week on datatalks.club for the week of "
                                                 f"{_long_date(publication_date)}."))
    return plan


def _events_footer():
    return (f"[See all upcoming events]({sources.ALL_EVENTS_URL}) · "
            f"[Add our events to your calendar]({sources.CALENDAR_URL})")


# --------------------------------------------------------------------------- creation

def create_issue_from_template(client, template_key, data, *, actor="", autofill=True):
    """Create an issue with the template's blocks; returns ``(issue, report)``.

    ``data`` takes the same keys as ``create_issue``. For ``dtc-weekly`` a
    missing ``publication_date`` defaults to the next Monday. Sources are
    fetched before the database transaction; the issue and its blocks are
    created atomically. ``report`` lists ``{block_id, type, label, status,
    note}`` with status ``auto``, ``placeholder`` or ``skipped`` (not created).
    """
    core = _core()
    _check_template(template_key)
    data = dict(data or {})
    today = _today()
    if template_key == "dtc-weekly" and not data.get("publication_date"):
        data["publication_date"] = next_monday(today).isoformat()
    # Validate before any network access, so bad input fails fast.
    key = core.normalize_issue_key(data.get("key") or data.get("subject") or "")
    fields = core.validate_issue_fields(data)
    if client.newsletter_issues.filter(key=key).exists():
        raise ApiValidationError({"key": "already_exists"}, status_code=409)

    plan = []
    if template_key == "dtc-weekly":
        with sources.fetch_budget(AUTOFILL_BUDGET_SECONDS):
            plan = _plan_dtc_weekly(fields, autofill=autofill, today=today)

    report = []
    with transaction.atomic():
        issue = core.create_issue(client, data, actor=actor)
        issue.template_key = template_key
        issue.save(update_fields=["template_key", "updated_at"])
        for item in plan:
            entry = {"block_id": None, "type": item["type"], "label": core.block_label(item["type"]),
                     "status": item["status"], "note": item["note"]}
            if item["status"] != "skipped":
                block = core.add_block(issue, item["type"], item["data"], actor=actor,
                                       needs_review=item["needs_review"], source=item["source"],
                                       source_note=item["source_note"])
                entry["block_id"] = block.pk
            report.append(entry)
    core.logger.info("newsletter issue created from template client=%s key=%s template=%s blocks=%s by=%s",
                     client.slug, issue.key, template_key, sum(1 for e in report if e["block_id"]), actor)
    return issue, report


# --------------------------------------------------------------------------- refill

REFILL_SOURCES = {
    "website:events": lambda day: sources.events_list_data(day),
    "website:recordings": lambda day: sources.recordings_data(day),
    "website:podcast": lambda day: sources.podcast_data(day),
    "website:books": lambda day: sources.book_data(day),
    "substack:alexey": lambda day: sources.alexey_article_data(day),
}


def refill_block(block, *, actor=""):
    """Re-fetch an auto-filled block's source and replace its data; returns the block.

    Multi-item sources (recordings, podcast) are matched by title, falling back
    to the first item. Fields the source does not provide (such as the summary
    the editor wrote for a recording) are kept.
    """
    core = _core()
    fetch = REFILL_SOURCES.get(block.source or "")
    if fetch is None:
        raise ApiValidationError({"block": "no_source"})
    issue = block.issue
    if issue.publication_date is None:
        raise ApiValidationError({"publication_date": "required"})
    fresh = fetch(issue.publication_date) if sources.autofill_enabled() else None
    if isinstance(fresh, list):
        title = (block.data or {}).get("title", "")
        fresh = next((item for item in fresh if item.get("title") == title), fresh[0] if fresh else None)
    if not fresh:
        raise ApiValidationError({"block": "source_unavailable"})
    merged = {name: value for name, value in (block.data or {}).items()
              if name in core.BLOCK_TYPES.get(block.block_type, {}).get("fields", []) and value}
    merged.update({name: value for name, value in fresh.items() if value})
    try:
        cleaned = core.validate_block(block.block_type, merged, draft=True)
    except ApiValidationError as exc:
        core.logger.warning("newsletter refill data rejected block=%s errors=%s", block.pk, exc.errors)
        raise ApiValidationError({"block": "source_unavailable"}) from exc
    with transaction.atomic():
        block.data = cleaned
        block.needs_review = _needs_input(block.block_type, cleaned)
        if block.block_type in {"recording", "podcast_episode"} and block.needs_review:
            block.source_note = RECORDING_NOTE
        else:
            block.source_note = (f"Filled automatically from {SOURCE_LABELS[block.source]} on {_note_date(_today())}. "
                                 "Check and edit freely.")
        block.updated_by = actor
        block.save()
        core._touch(issue, actor)
    return block
