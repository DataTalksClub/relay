import hashlib
import secrets
from dataclasses import dataclass

from django.core import signing
from django.db import transaction

from mailing.models import CampaignRecipient

TOKEN_BYTES = 32

# Double opt-in verification tokens are stateless: the scope is signed into
# the token instead of stored, so no model or migration is involved. The
# payload carries only ids and the category name, never a raw email, so the
# confirm URL stays free of addresses.
SUBSCRIPTION_VERIFICATION_SALT = "mailing.subscriptions.category_verification"
SUBSCRIPTION_VERIFICATION_TOKEN_MAX_AGE_SECONDS = 60 * 60 * 48


@dataclass(frozen=True)
class CampaignRecipientTokens:
    tracking_token: str | None
    unsubscribe_token: str | None


def issue_subscription_verification_token(*, contact_id, audience_id, client_id, category):
    payload = {
        "contact_id": contact_id,
        "audience_id": audience_id,
        "client_id": client_id,
        "category": category,
    }
    return signing.TimestampSigner(salt=SUBSCRIPTION_VERIFICATION_SALT).sign_object(payload)


def read_subscription_verification_token(token):
    """Return the signed payload for a valid, unexpired token, else None.

    Any failure -- malformed, tampered, foreign salt, or expired -- returns
    None so callers fail closed with a single opaque error.
    """
    if not isinstance(token, str) or not token.strip():
        return None
    signer = signing.TimestampSigner(salt=SUBSCRIPTION_VERIFICATION_SALT)
    try:
        payload = signer.unsign_object(
            token,
            max_age=SUBSCRIPTION_VERIFICATION_TOKEN_MAX_AGE_SECONDS,
        )
    except signing.BadSignature:
        return None
    if not isinstance(payload, dict):
        return None
    expected_keys = {"contact_id", "audience_id", "client_id", "category"}
    if set(payload) != expected_keys or not all(isinstance(payload[key], int) for key in expected_keys - {"category"}):
        return None
    if not isinstance(payload["category"], str):
        return None
    return payload


def token_hash(raw_token):
    try:
        token_bytes = raw_token.encode("ascii")
    except UnicodeEncodeError:
        return None
    return hashlib.sha256(token_bytes).hexdigest()


def generate_raw_token():
    return secrets.token_urlsafe(TOKEN_BYTES)


@transaction.atomic
def ensure_campaign_recipient_tokens(recipient):
    recipient = CampaignRecipient.objects.select_for_update().get(pk=recipient.pk)
    tracking_token = None
    unsubscribe_token = None

    updates = []
    if not recipient.tracking_token_hash:
        tracking_token = generate_raw_token()
        tracking_hash = token_hash(tracking_token)
        recipient.tracking_token_hash = tracking_hash
        updates.append("tracking_token_hash")
    if not recipient.unsubscribe_token_hash:
        unsubscribe_token = generate_raw_token()
        unsubscribe_hash = token_hash(unsubscribe_token)
        recipient.unsubscribe_token_hash = unsubscribe_hash
        updates.append("unsubscribe_token_hash")
    if updates:
        recipient.save(update_fields=[*updates, "updated_at"])

    return CampaignRecipientTokens(
        tracking_token=tracking_token,
        unsubscribe_token=unsubscribe_token,
    )


def get_recipient_by_tracking_token(raw_token):
    return _get_recipient_by_token_hash(raw_token, "tracking_token_hash")


def get_recipient_by_unsubscribe_token(raw_token):
    return _get_recipient_by_token_hash(raw_token, "unsubscribe_token_hash")


def _get_recipient_by_token_hash(raw_token, hash_field):
    if not raw_token:
        return None
    hashed_token = token_hash(raw_token)
    if hashed_token is None:
        return None
    return (
        CampaignRecipient.objects.select_related(
            "campaign",
            "campaign__client",
            "campaign__audience",
            "contact",
        )
        .filter(**{hash_field: hashed_token})
        .first()
    )
