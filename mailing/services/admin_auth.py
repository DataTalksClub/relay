import secrets

from django.core.exceptions import ValidationError
from django.utils import timezone

from mailing.models import AdminApiKey
from mailing.services.auth import check_api_key, hash_api_key

ADMIN_KEY_PREFIX = "relay_admin_"


def create_admin_api_key(*, user, name):
    if not user.is_active or not user.is_staff:
        raise ValidationError("Admin keys require an active staff user.")
    public_id = secrets.token_hex(8)
    raw_key = f"{ADMIN_KEY_PREFIX}{public_id}_{secrets.token_urlsafe(32)}"
    key = AdminApiKey(user=user, name=name, public_id=public_id, key_hash=hash_api_key(raw_key))
    key.full_clean()
    key.save()
    return key, raw_key


def authenticate_admin_request(request):
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token.startswith(ADMIN_KEY_PREFIX):
        return None
    public_id, separator, secret = token[len(ADMIN_KEY_PREFIX) :].partition("_")
    if not separator or not secret:
        return None
    key = (
        AdminApiKey.objects.select_related("user")
        .filter(public_id=public_id, revoked_at__isnull=True, user__is_active=True, user__is_staff=True)
        .first()
    )
    if key is None or not check_api_key(token, key.key_hash):
        return None
    AdminApiKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
    return key
