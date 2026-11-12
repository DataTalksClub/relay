"""Shared campaign suppression rules.

Single source of truth for "must this contact not be mailed": the snapshot
annotations (``_annotate_snapshot_skip`` in campaigns.py), the estimate/bulk
path (``_skip_reason_bulk``) and the right-before-SES recheck in
campaign_sender.py all implement these rules. Keep them here, not duplicated.
"""

from mailing.models import CampaignRecipientSkipReason, SubscriptionStatus
from mailing.services.contacts import has_invalid_email_validation


def _subscription_status(subscription):
    if subscription is None:
        return None
    if isinstance(subscription, dict):
        return subscription.get("status")
    return subscription.status


def _subscription_verified_at(subscription):
    if subscription is None:
        return None
    if isinstance(subscription, dict):
        return subscription.get("verified_at")
    return subscription.verified_at


def suppression_skip_reason(contact, audience_subscription, client_subscription, *, category_disabled=False):
    """Return the snapshot skip reason for a contact, or "" when sendable.

    ``audience_subscription``/``client_subscription`` accept either Subscription
    instances, bulk-query dicts with ``status``/``verified_at`` keys, or None.
    """
    reason = CampaignRecipientSkipReason
    if contact.hard_bounced_at is not None:
        return reason.HARD_BOUNCE
    if contact.complained_at is not None:
        return reason.COMPLAINT
    if contact.global_unsubscribed_at is not None:
        return reason.GLOBAL_UNSUBSCRIBE
    if has_invalid_email_validation(contact):
        return reason.INVALID_EMAIL
    if not _is_verified(contact, audience_subscription, client_subscription):
        return reason.UNVERIFIED
    if _subscription_status(audience_subscription) == SubscriptionStatus.UNSUBSCRIBED:
        return reason.AUDIENCE_UNSUBSCRIBE
    if _subscription_status(client_subscription) == SubscriptionStatus.UNSUBSCRIBED:
        return reason.CLIENT_UNSUBSCRIBE
    if _subscription_status(client_subscription) != SubscriptionStatus.SUBSCRIBED:
        return reason.SUPPRESSED
    if category_disabled:
        return reason.CLIENT_UNSUBSCRIBE
    return ""


def _is_verified(contact, audience_subscription, client_subscription):
    """Mirror contacts.is_verified_for_marketing without per-row queries."""
    if contact.verified_at is not None:
        return True
    for subscription in (audience_subscription, client_subscription):
        if _subscription_verified_at(subscription) is not None:
            return True
    return False
