"""Per-recipient merge vars for campaigns: ``{{ name }}`` in the HTML/text body.

Values come from the campaign's recipient list: each member's
``metadata["merge_vars"][name]`` (a non-empty string without CR/LF).

Rendering stays once per batch: every tag is swapped for a fixed ASCII
sentinel before the body is rendered (link rewriting, MIME build), and the
per-recipient value is byte-substituted afterwards, HTML-escaped in the HTML
part and raw in the text part. A missing or empty value fails loudly: queueing
is refused before anything is sent, and a value that disappears after queueing
fails that recipient without sending. A literal ``{{ ... }}`` never ships.
"""

import logging
import re
from html import escape

from mailing.models import RecipientListMember

logger = logging.getLogger(__name__)

MERGE_TAG_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_ANY_BRACES_RE = re.compile(r"\{\{|\}\}")
MERGE_VARS_KEY = "merge_vars"
_ISSUE_SAMPLE = 5


class MergeVarError(Exception):
    """A merge var is malformed, unknown, or empty for a recipient."""


def html_sentinel(name):
    return f"RLYMRGH0{name}0SENTINEL"


def text_sentinel(name):
    return f"RLYMRGT0{name}0SENTINEL"


def merge_var_names(campaign):
    """Sorted merge var names used in the campaign's HTML and text bodies."""
    names = set()
    for body in (campaign.html_body or "", campaign.text_body or ""):
        names.update(MERGE_TAG_RE.findall(body))
    return tuple(sorted(names))


def merge_tag_issues(campaign):
    """Static problems: malformed braces, or merge tags in the subject."""
    issues = []
    if "{{" in (campaign.subject or "") or "}}" in (campaign.subject or ""):
        issues.append("Merge tags are not supported in the subject; remove {{ ... }} from it.")
    for label, body in (("HTML", campaign.html_body or ""), ("plain-text", campaign.text_body or "")):
        if _ANY_BRACES_RE.search(MERGE_TAG_RE.sub("", body)):
            issues.append(f"The {label} body has a malformed merge tag; use {{{{ name }}}} with letters, digits and _.")
    if merge_var_names(campaign) and not campaign.recipient_list_key:
        issues.append("Merge tags need a recipient list whose members carry metadata.merge_vars.")
    return issues


def with_sentinels(html_body, text_body):
    html = MERGE_TAG_RE.sub(lambda m: html_sentinel(m.group(1)), html_body or "")
    text = MERGE_TAG_RE.sub(lambda m: text_sentinel(m.group(1)), text_body or "")
    return html, text


def member_merge_values(metadata, names):
    """Validated ``{name: value}`` for one member; raises MergeVarError."""
    merge_vars = (metadata or {}).get(MERGE_VARS_KEY)
    if not isinstance(merge_vars, dict):
        merge_vars = {}
    values = {}
    missing = []
    for name in names:
        value = merge_vars.get(name)
        if not isinstance(value, str) or not value.strip() or "\r" in value or "\n" in value:
            missing.append(name)
            continue
        if "RLYMRG" in value or "SENTINEL" in value:
            raise MergeVarError(f"merge var {name!r} value contains a template sentinel")
        values[name] = value
    if missing:
        raise MergeVarError(f"missing or empty merge var(s): {', '.join(missing)}")
    return values


def substitute(raw, values, *, html_escape_html=True):
    """Replace merge sentinels in ``raw`` (str or bytes) with recipient values."""
    is_bytes = isinstance(raw, bytes)
    for name, value in values.items():
        html_value = escape(value, quote=True) if html_escape_html else value
        pairs = ((html_sentinel(name), html_value), (text_sentinel(name), value))
        for sentinel, replacement in pairs:
            if is_bytes:
                raw = raw.replace(sentinel.encode("ascii"), replacement.encode("utf-8"))
            else:
                raw = raw.replace(sentinel, replacement)
    leftover = b"RLYMRG" if is_bytes else "RLYMRG"
    if leftover in raw:
        raise MergeVarError("unsubstituted merge sentinel remains after rendering")
    return raw


def member_metadata_by_contact(campaign, contact_ids):
    """``{contact_id: metadata}`` for active members of the campaign's list."""
    rows = RecipientListMember.objects.filter(
        recipient_list__audience_id=campaign.audience_id,
        recipient_list__client_id=campaign.client_id,
        recipient_list__key=campaign.recipient_list_key,
        active=True,
        contact_id__in=list(contact_ids),
    ).values_list("contact_id", "metadata")
    return dict(rows)


def pending_recipient_issues(campaign, contact_ids):
    """Queue-time check over every pending recipient; returns issue strings."""
    names = merge_var_names(campaign)
    if not names:
        return []
    metadata = member_metadata_by_contact(campaign, contact_ids)
    bad = []
    for contact_id in contact_ids:
        try:
            member_merge_values(metadata.get(contact_id), names)
        except MergeVarError as exc:
            bad.append((contact_id, str(exc)))
    if not bad:
        return []
    sample = "; ".join(f"contact {cid}: {err}" for cid, err in bad[:_ISSUE_SAMPLE])
    logger.error(
        "campaign_merge_vars_invalid campaign_id=%s vars=%s bad_recipients=%d sample=%s",
        campaign.pk, ",".join(names), len(bad), sample,
    )
    return [
        f"{len(bad)} recipient(s) are missing merge vars ({', '.join(names)}); nothing was sent. "
        f"Sample: {sample}."
    ]
