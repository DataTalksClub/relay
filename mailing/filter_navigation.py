"""Remove one filter while preserving the rest of an investigation."""
from mailing.services.operator_ui import active_contact_filters

FILTER_PARAMETERS = {
    "Email": "q", "Audience": "audience", "Subscription": "subscription_status",
    "Verification": "verified", "Validation": "email_validation_status", "Suppression": "suppression",
    "Campaign": "campaign_status", "Skip reason": "skip_reason", "Engagement": "engagement",
    "Includes tag": "include_tags", "Excludes tag": "exclude_tags",
}


def removable_contact_filters(request, filters):
    rows = []
    for item in active_contact_filters(filters):
        params = request.GET.copy()
        params.pop("page", None)
        field = FILTER_PARAMETERS.get(item.label)
        if field:
            if field in {"include_tags", "exclude_tags"}:
                params.setlist(field, [value for value in params.getlist(field) if value != item.value])
            else:
                params.pop(field, None)
            if field == "engagement":
                params.pop("inactive_since", None)
        rows.append({"label": item.label, "value": item.value, "remove_url": "?" + params.urlencode() if field else ""})
    return rows
