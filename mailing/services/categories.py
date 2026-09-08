"""Canonical subscription category vocabulary.

Category names double as ``CategoryPreference.tag`` values, so a canonical
category is optable out through the same tag path a free-form ``category_tag``
uses at send time. The ``transactional`` category is always on: a contact
cannot opt out of it, and a send tagged with it is never suppressed by the
category check.
"""

from urllib.parse import quote

from django.conf import settings

from mailing.services.api_errors import ApiValidationError

CANONICAL_CATEGORIES = ("newsletter", "events", "courses", "product", "transactional")
TRANSACTIONAL_CATEGORY = "transactional"

_CANONICAL_CATEGORY_SET = frozenset(CANONICAL_CATEGORIES)


def is_canonical_category(value):
    return value in _CANONICAL_CATEGORY_SET


def validate_canonical_category(value, field="category"):
    """Return the canonical category name or raise a validation error."""
    if value in (None, ""):
        raise ApiValidationError({field: "required"})
    if not isinstance(value, str):
        raise ApiValidationError({field: "must_be_string"})
    category = value.strip()
    if not is_canonical_category(category):
        raise ApiValidationError({field: "unknown"})
    return category


def category_label(category):
    return category.replace("-", " ").title()


def subscription_confirm_url(token):
    """Build the double opt-in confirm link for the site's public landing page.

    ``SUBSCRIPTION_CONFIRM_BASE_URL`` is the landing page URL without the
    token query parameter; the token is always appended as ``token=...`` so a
    raw email never appears in the URL.
    """
    base = settings.SUBSCRIPTION_CONFIRM_BASE_URL.rstrip("/")
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}token={quote(token, safe='')}"
