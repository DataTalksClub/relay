"""Safety guards for campaign test sends: recipient allowlist and non-empty body."""

import html
import re

from mailing.services.api_errors import ApiValidationError
from mailing.services.campaign_merge import merge_tag_issues
from mailing.services.campaign_template_rendering import render_campaign_fields
from mailing.services.contacts import normalize_email

ALLOWED_TEST_RECIPIENT_HINT = "dev+...@dtcdev.click or *@simulator.amazonses.com"

_ALLOWED_TEST_RECIPIENT_RE = re.compile(
    r"^(?:dev\+[^@\s]+@dtcdev\.click|[^@\s]+@simulator\.amazonses\.com)$"
)
_STYLE_OR_SCRIPT_RE = re.compile(r"<(style|script)\b[^>]*>.*?</\1>", flags=re.IGNORECASE | re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", flags=re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def is_allowed_test_recipient(email):
    """Allow test sends only to dev+...@dtcdev.click or SES simulator mailboxes."""
    if not isinstance(email, str):
        return False
    normalized = normalize_email(email)
    return bool(_ALLOWED_TEST_RECIPIENT_RE.fullmatch(normalized))


def ensure_allowed_test_recipient(email):
    normalized = normalize_email(email) if isinstance(email, str) else ""
    if not is_allowed_test_recipient(normalized):
        raise ApiValidationError(
            {
                "email": (
                    f"Test sends are only allowed to {ALLOWED_TEST_RECIPIENT_HINT}; "
                    f"refused {normalized or '<empty>'}."
                )
            }
        )
    return normalized


def _visible_html_text(html_body):
    if not html_body:
        return ""
    stripped = _STYLE_OR_SCRIPT_RE.sub(" ", html_body)
    stripped = _HTML_COMMENT_RE.sub(" ", stripped)
    stripped = _HTML_TAG_RE.sub(" ", stripped)
    unescaped = html.unescape(stripped).replace("\xa0", " ")
    return " ".join(unescaped.split())


def has_meaningful_body(html_body, text_body):
    """True when the campaign carries visible text beyond an empty/footer-only shell."""
    if (text_body or "").strip():
        return True
    return bool(_visible_html_text(html_body or ""))


def ensure_test_sendable_campaign(campaign):
    """Refuse test-sending drafts with missing subject, empty/footer-only body, or broken tags."""
    if not (campaign.subject or "").strip():
        raise ApiValidationError({"subject": "Add a subject before sending a test."})
    tag_issues = merge_tag_issues(campaign)
    if tag_issues:
        raise ApiValidationError({"merge_vars": " ".join(tag_issues)})
    try:
        fields = render_campaign_fields(campaign)
    except ValueError as exc:
        raise ApiValidationError({"template_key": str(exc)}) from exc
    if not has_meaningful_body(fields.get("html_body"), fields.get("text_body")):
        raise ApiValidationError(
            {"body": "Campaign body is empty; add HTML or plain-text content before sending a test."}
        )


def synthetic_test_merge_values(names):
    """Deterministic, safe placeholder values when a test address is not on the recipient list."""
    values = {}
    for name in names:
        lower = name.lower()
        if lower.endswith(("_url", "_link")) or lower in {"url", "link"}:
            values[name] = f"https://example.invalid/test-merge/{name}"
        else:
            values[name] = f"[test:{name}]"
    return values
