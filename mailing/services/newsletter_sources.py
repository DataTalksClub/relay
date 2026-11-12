"""Public content sources for auto-filling newsletter blocks.

Pure fetch-and-map helpers: no database access. Each public function returns
plain dicts ready to be used as newsletter block ``data`` (or None / [] when
there is nothing to use). They never raise: network or parsing problems are
logged as warnings on ``mailing.newsletters`` and treated as "no data".

Every HTTP request goes through ``_http_get`` so tests can patch one function.
Responses are cached in Django's cache for ``CACHE_SECONDS``.
"""

import datetime as dt
import functools
import hashlib
import html
import json
import logging
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import frontmatter
import yaml
from django.core.cache import cache

logger = logging.getLogger("mailing.newsletters")

USER_AGENT = "relay-newsletter"
TIMEOUT_SECONDS = 6
CACHE_SECONDS = 600
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
EVENT_ZONE = ZoneInfo("Europe/Berlin")
SITE_URL = "https://datatalks.club/"
ALL_EVENTS_URL = "https://lu.ma/dtc-events"
CALENDAR_URL = ("https://calendar.google.com/calendar/?cid="
                "ZjhxaWRqbnEwamhzY3A4ODA5azFlZ2hzNjBAZ3JvdXAuY2FsZW5kYXIuZ29vZ2xlLmNvbQ")

DEFAULT_WEBSITE_RAW = "https://raw.githubusercontent.com/DataTalksClub/datatalksclub.github.io/main"
DEFAULT_WEBSITE_API = "https://api.github.com/repos/DataTalksClub/datatalksclub.github.io/contents"
DEFAULT_SUBSTACK_FEED = "https://alexeyondata.substack.com/feed"


def website_raw():
    return os.environ.get("RELAY_NEWSLETTER_WEBSITE_RAW", DEFAULT_WEBSITE_RAW).rstrip("/")


def website_api():
    return os.environ.get("RELAY_NEWSLETTER_WEBSITE_API", DEFAULT_WEBSITE_API).rstrip("/")


def substack_feed():
    return os.environ.get("RELAY_NEWSLETTER_SUBSTACK_FEED", DEFAULT_SUBSTACK_FEED)


def autofill_enabled():
    return os.environ.get("RELAY_NEWSLETTER_AUTOFILL", "1").strip().lower() not in {"0", "false", "no", "off", ""}


def _never_raise(default):
    """Public source functions log and return ``default`` instead of raising."""

    def decorate(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - autofill must never break issue creation
                logger.warning("newsletter source %s failed error=%s: %s", func.__name__, type(exc).__name__, exc)
                return default() if callable(default) else default
        return wrapper

    return decorate


# --------------------------------------------------------------------------- HTTP

def _http_get(url):
    """GET ``url`` and return the body, or None on any failure. Tests patch this."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - configured https URLs
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except Exception as exc:  # noqa: BLE001 - autofill must never break issue creation
        logger.warning("newsletter source fetch failed url=%s error=%s: %s", url, type(exc).__name__, exc)
        return None
    if len(body) > MAX_RESPONSE_BYTES:
        logger.warning("newsletter source fetch too large url=%s", url)
        return None
    return body


def _fetch(url):
    """Cached ``_http_get``; only successful responses are cached."""
    if not autofill_enabled():
        return None
    key = "newsletter-source:" + hashlib.sha256(url.encode()).hexdigest()
    try:
        cached = cache.get(key)
    except Exception:  # noqa: BLE001 - a broken cache must not break autofill
        cached = None
    if cached is not None:
        return cached
    try:
        body = _http_get(url)
    except Exception as exc:  # noqa: BLE001 - defensive: patched/replaced fetchers may raise
        logger.warning("newsletter source fetch failed url=%s error=%s", url, exc)
        body = None
    if not isinstance(body, bytes | bytearray) or not body:
        return None
    try:
        cache.set(key, bytes(body), CACHE_SECONDS)
    except Exception:  # noqa: BLE001
        pass
    return bytes(body)


def _text(url):
    body = _fetch(url)
    if body is None:
        return None
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("newsletter source is not UTF-8 url=%s", url)
        return None


# --------------------------------------------------------------------------- formatting helpers

def _join_names(names):
    names = [name for name in names if name]
    if len(names) <= 1:
        return "".join(names)
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def _short_date(value):
    return f"{value:%b} {value.day}"


def _clean_url(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or any(ord(c) < 33 for c in value):
        return ""
    return value


def _clip(value, limit):
    value = (value or "").strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _trim_sentences(text, limit=600):
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = [match.end() for match in re.finditer(r"[.!?](?=\s|$)", cut)]
    if ends and ends[-1] > limit // 3:
        return cut[: ends[-1]].strip()
    return cut.rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _strip_html(value):
    text = re.sub(r"<(br|/p|/li|/h\d)\s*/?>", "\n", value or "", flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines() if line.strip())


def _youtube_id(url):
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if host.endswith("youtu.be"):
        return parsed.path.strip("/").split("/")[0]
    if "youtube.com" in host:
        if parsed.path.startswith(("/embed/", "/live/", "/shorts/")):
            return parsed.path.split("/")[2]
        return (parse_qs(parsed.query).get("v") or [""])[0]
    return ""


def _youtube_thumbnail(url):
    video_id = _youtube_id(url)
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id or ""):
        return ""
    return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"


# --------------------------------------------------------------------------- people

def speaker_name(slug):
    """Display name for a ``_people/<slug>.md`` entry; falls back to the slug."""
    slug = str(slug or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", slug):
        return slug
    text = _text(f"{website_raw()}/_people/{slug}.md")
    if not text:
        return slug
    try:
        title = frontmatter.loads(text).metadata.get("title")
    except Exception as exc:  # noqa: BLE001
        logger.warning("newsletter people front matter unreadable slug=%s error=%s", slug, exc)
        return slug
    return str(title).strip() if title else slug


def _speaker_names(slugs):
    if isinstance(slugs, str):
        slugs = [slugs]
    if not isinstance(slugs, list):
        return []
    return [speaker_name(slug) for slug in slugs if slug]


# --------------------------------------------------------------------------- events

def _parse_event_time(value):
    if isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, dt.date):
        moment = dt.datetime.combine(value, dt.time())
    elif isinstance(value, str):
        value = value.strip()
        moment = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                moment = dt.datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        if moment is None:
            return None
    else:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=EVENT_ZONE)
    return moment.astimezone(EVENT_ZONE)


@_never_raise(list)
def all_events():
    """Every usable entry of ``_data/events.yaml`` with ``time`` as an aware datetime."""
    text = _text(f"{website_raw()}/_data/events.yaml")
    if not text:
        return []
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.warning("newsletter events.yaml unreadable error=%s", exc)
        return []
    if not isinstance(raw, list):
        logger.warning("newsletter events.yaml is not a list")
        return []
    events = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        moment = _parse_event_time(entry.get("time"))
        title = str(entry.get("title") or "").strip()
        if moment is None or not title:
            continue
        events.append({
            "time": moment,
            "title": title,
            "type": str(entry.get("type") or "").strip().lower(),
            "speakers": entry.get("speakers") or [],
            "link": _clean_url(entry.get("link")),
            "youtube": _clean_url(entry.get("youtube")),
        })
    events.sort(key=lambda item: item["time"])
    return events


def _day_start(day):
    return dt.datetime.combine(day, dt.time(), tzinfo=EVENT_ZONE)


@_never_raise(list)
def upcoming_events(publication_date, *, days=21, limit=8):
    """Events from ``publication_date`` 00:00 (Berlin) for ``days`` days, ascending."""
    start = _day_start(publication_date)
    end = start + dt.timedelta(days=days)
    return [event for event in all_events() if start <= event["time"] < end][:limit]


def _event_type_label(value):
    value = (value or "").replace("_", " ").replace("-", " ").strip()
    return value[:1].upper() + value[1:] if value else "Event"


def event_when(moment):
    """``Mon, November 16 · 5:00 PM CET`` — the wording issue #302 used."""
    hour = moment.hour % 12 or 12
    return f"{moment:%a}, {moment:%B} {moment.day} · {hour}:{moment:%M} {'AM' if moment.hour < 12 else 'PM'} {moment:%Z}"


def event_line(event):
    speakers = _join_names(_speaker_names(event.get("speakers")))
    parts = [f"{event_when(event['time'])} · {_event_type_label(event['type'])}", event["title"].replace("|", "/"),
             event.get("link") or ""]
    if speakers:
        parts.append(f"with {speakers}".replace("|", "/"))
    return " | ".join(parts)


@_never_raise(None)
def events_list_data(publication_date):
    events = upcoming_events(publication_date)
    if not events:
        return None
    return {
        "kicker": "EVENTS",
        "title": "Upcoming Events",
        "events": "\n".join(event_line(event) for event in events),
        "footer": f"[See all upcoming events]({ALL_EVENTS_URL}) · [Add our events to your calendar]({CALENDAR_URL})",
    }


def _past_events(publication_date, days, *, podcast):
    end = _day_start(publication_date)
    start = end - dt.timedelta(days=days)
    return [event for event in all_events()
            if start <= event["time"] < end and event["youtube"] and (event["type"] == "podcast") == podcast]


def _recording_card(event, *, kind, cta_text):
    speakers = _join_names(_speaker_names(event.get("speakers")))
    meta = f"{kind} · {_short_date(event['time'])}" + (f" · with {speakers}" if speakers else "")
    return {
        "title": _clip(f"{kind} Recording: {event['title']}", 500),
        "meta": _clip(meta, 500),
        "image_url": _youtube_thumbnail(event["youtube"]),
        "image_link": event["youtube"],
        "cta_text": cta_text,
        "cta_url": event["youtube"],
    }


@_never_raise(list)
def recordings_data(publication_date, *, days=7):
    """``recording`` block data for non-podcast events with a recording in the past ``days``."""
    return [_recording_card(event, kind=_event_type_label(event["type"]), cta_text="Watch the recording")
            for event in _past_events(publication_date, days, podcast=False)]


@_never_raise(list)
def podcast_data(publication_date, *, days=7):
    """``podcast_episode`` block data for podcast events with a recording in the past ``days``."""
    return [_recording_card(event, kind="Podcast", cta_text="Listen to the episode")
            for event in _past_events(publication_date, days, podcast=True)]


# --------------------------------------------------------------------------- books

def _as_date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _first_paragraph(markdown_body):
    for paragraph in re.split(r"\n\s*\n", markdown_body or ""):
        paragraph = paragraph.strip()
        if paragraph and not paragraph.startswith(("#", "<", "!", "{%", "---")):
            return _trim_sentences(paragraph, 600)
    return ""


@_never_raise(None)
def book_data(publication_date):
    """``book_of_week`` data for the book whose start..end overlaps the issue's week (Mon–Sun)."""
    week_start = publication_date
    week_end = publication_date + dt.timedelta(days=6)
    text = _text(f"{website_api()}/_books")
    if not text:
        return None
    try:
        listing = json.loads(text)
    except ValueError:
        logger.warning("newsletter book listing is not JSON")
        return None
    if not isinstance(listing, list):
        return None
    candidates = []
    for item in listing:
        name = item.get("name") if isinstance(item, dict) else None
        match = re.fullmatch(r"(\d{8})-[A-Za-z0-9_.-]+\.md", name or "")
        if not match:
            continue
        try:
            named = dt.datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        # Book weeks start on the date in the file name; look a month back for long runs.
        if week_start - dt.timedelta(days=31) <= named <= week_end:
            candidates.append((named, name))
    for _named, name in sorted(candidates, reverse=True):
        source = _text(f"{website_raw()}/_books/{name}")
        if not source:
            continue
        try:
            post = frontmatter.loads(source)
        except Exception as exc:  # noqa: BLE001
            logger.warning("newsletter book front matter unreadable name=%s error=%s", name, exc)
            continue
        meta = post.metadata
        start, end = _as_date(meta.get("start")), _as_date(meta.get("end"))
        if start is None:
            continue
        end = end or start + dt.timedelta(days=4)
        if start > week_end or end < week_start:
            continue
        title = str(meta.get("title") or "").strip()
        if not title:
            continue
        page = f"{SITE_URL}books/{name.removesuffix('.md')}.html"
        authors = meta.get("authors") or []
        names = _join_names(_speaker_names(authors))
        cover = str(meta.get("cover") or "").strip()
        cover_url = _clean_url(cover if cover.startswith("http") else SITE_URL + cover.lstrip("/")) if cover else ""
        return {
            "title": _clip(f"Book of the Week: {title}", 500),
            "meta": _clip(f"by {names}", 500) if names else "",
            "image_url": cover_url,
            "image_link": page,
            "body": _first_paragraph(post.content),
            "cta_text": "Learn more",
            "cta_url": page,
        }
    return None


# --------------------------------------------------------------------------- Substack

def _child_text(item, tag):
    child = item.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


@_never_raise(list)
def substack_posts():
    """Feed items as ``{title, link, description, published, image}``, newest first."""
    body = _fetch(substack_feed())
    if body is None:
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        logger.warning("newsletter substack feed unreadable error=%s", exc)
        return []
    posts = []
    for item in root.iter("item"):
        if len(posts) >= 20:
            break
        try:
            published = parsedate_to_datetime(_child_text(item, "pubDate"))
        except (TypeError, ValueError):
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=dt.UTC)
        enclosure = item.find("enclosure")
        image = ""
        if enclosure is not None and str(enclosure.get("type", "")).startswith("image/"):
            image = _clean_url(enclosure.get("url"))
        link = _clean_url(_child_text(item, "link"))
        title = _strip_html(_child_text(item, "title"))
        if not (link and title):
            continue
        posts.append({"title": title, "link": link, "description": _strip_html(_child_text(item, "description")),
                      "published": published, "image": image})
    posts.sort(key=lambda post: post["published"], reverse=True)
    return posts


@_never_raise(None)
def alexey_article_data(publication_date, *, days=8):
    """Latest Substack post published in the ``days`` before ``publication_date`` (inclusive)."""
    start = publication_date - dt.timedelta(days=days)
    for post in substack_posts():
        published = post["published"].astimezone(EVENT_ZONE).date()
        if start <= published <= publication_date:
            return {
                "title": _clip(f"From Alexey on Data: {post['title']}", 500),
                "image_url": post["image"],
                "image_link": post["link"],
                "body": _trim_sentences(post["description"], 2000),
                "cta_text": "Read the article",
                "cta_url": post["link"],
            }
    return None
