"""Email substring and domain lookups over ``Contact.normalized_email``.

Domain lookups are suffix matches. PostgreSQL serves them from the functional
btree index ``contacts_norm_email_rev_idx`` on
``reverse(normalized_email) text_pattern_ops`` (migration 0060): a suffix
``LIKE '%@fsu.edu'`` becomes the prefix ``LIKE 'ude.usf@%'`` on the reversed
email, which is an index range scan instead of a scan of every contact.

Substring lookups (``contains``) match anywhere in the address with
``LIKE '%x%'`` on the casefolded column. That is one sequential pass over the
contacts table inside PostgreSQL (no per-row Python work), which stays well
under a second at Relay's contact volume. Prefer ``domain`` for domains.
"""

from __future__ import annotations

import re

from django.db.models import Q, QuerySet
from django.db.models.functions import Reverse

REVERSED_EMAIL_ANNOTATION = "normalized_email_reversed"
MAX_DOMAIN_LENGTH = 253
MAX_CONTAINS_LENGTH = 320
DOMAIN_PATTERN = re.compile(r"^[^\s@]+$")


class EmailSearchError(ValueError):
    """Raised for an unusable search term; ``field`` names the offending input."""

    def __init__(self, field: str, code: str):
        super().__init__(f"{field}: {code}")
        self.field = field
        self.code = code


def normalize_domain(value) -> str:
    """Return a casefolded domain without a leading ``@`` (``""`` when blank)."""
    domain = str(value or "").strip().casefold()
    domain = domain.removeprefix("@").strip(".")
    if not domain:
        return ""
    if len(domain) > MAX_DOMAIN_LENGTH:
        raise EmailSearchError("domain", "too_long")
    if not DOMAIN_PATTERN.match(domain):
        raise EmailSearchError("domain", "invalid")
    return domain


def normalize_contains(value) -> str:
    term = str(value or "").strip().casefold()
    if len(term) > MAX_CONTAINS_LENGTH:
        raise EmailSearchError("email_contains", "too_long")
    return term


def filter_email_domain(queryset: QuerySet, domain: str) -> QuerySet:
    """Contacts at ``domain`` or any of its subdomains (``x@fsu.edu``, ``x@cs.fsu.edu``)."""
    if not domain:
        return queryset
    exact = f"@{domain}"[::-1]
    subdomain = f".{domain}"[::-1]
    return queryset.alias(**{REVERSED_EMAIL_ANNOTATION: Reverse("normalized_email")}).filter(
        Q(**{f"{REVERSED_EMAIL_ANNOTATION}__startswith": exact})
        | Q(**{f"{REVERSED_EMAIL_ANNOTATION}__startswith": subdomain})
    )


def filter_email_contains(queryset: QuerySet, term: str) -> QuerySet:
    if not term:
        return queryset
    return queryset.filter(normalized_email__contains=term)
