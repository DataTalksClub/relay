"""Presentation-only template filters."""

from django import template

register = template.Library()


@register.filter
def trim_number(value):
    """Render whole floats without a redundant '.0'; keep meaningful fractions (e.g. 0.5, 14.3)."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return value


@register.filter
def verified_domains(identities):
    """Names of DOMAIN identities whose verification status is SUCCESS (never emails, never inferred)."""
    return [
        item["name"]
        for item in identities or []
        if item.get("type") == "domain" and str(item.get("verification", "")).lower() == "success"
    ]
