import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db.models import Q
from django.utils import timezone

from mailing.models import Client, ClientApiKey

API_KEY_PREFIX = "relay_"
API_KEY_USAGE_WRITE_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True)
class AuthResult:
    client: Client | None
    error: str | None = None
    status_code: int = 401

    @property
    def is_authenticated(self):
        return self.client is not None


def hash_api_key(raw_api_key):
    return make_password(raw_api_key)


def check_api_key(raw_api_key, api_key_hash):
    if not raw_api_key or not api_key_hash:
        return False
    return check_password(raw_api_key, api_key_hash)


def fast_hash_api_key(raw_api_key):
    """Fast verifier for high-entropy API keys (intake e5cd1579).

    Raw keys carry ~192 bits of entropy (`secrets.token_urlsafe(32)`), so the
    ~240 ms PBKDF2 stretching designed for low-entropy passwords is pure
    per-request overhead (measured on prod: every authenticated API call paid
    it). HMAC-SHA256 keyed by `SECRET_KEY` with a constant-time comparison is
    the fast verifier here. PBKDF2 remains only for rows that have not yet
    stored a fast verifier; a token that does not match a fast verifier never
    triggers a stretching operation.
    """
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        raw_api_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def check_api_key_fast(raw_api_key, stored_fast_hash):
    if not raw_api_key or not stored_fast_hash:
        return False
    try:
        return hmac.compare_digest(fast_hash_api_key(raw_api_key), stored_fast_hash)
    except (TypeError, ValueError):
        return False


def generate_raw_api_key(public_id=None):
    public_id = public_id or secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
    secret = secrets.token_urlsafe(32)
    return f"{API_KEY_PREFIX}{public_id}_{secret}"


def public_id_from_raw_key(raw_api_key):
    if not raw_api_key.startswith(API_KEY_PREFIX):
        return ""
    remainder = raw_api_key[len(API_KEY_PREFIX) :]
    public_id, separator, _secret = remainder.partition("_")
    if not separator or not public_id:
        return ""
    return public_id


def create_client_api_key(*, client, name, notes="", raw_api_key=None, public_id=None):
    raw_key = raw_api_key or generate_raw_api_key(public_id=public_id)
    parsed_public_id = public_id or public_id_from_raw_key(raw_key)
    if not parsed_public_id:
        parsed_public_id = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
    api_key = ClientApiKey.objects.create(
        client=client,
        name=name,
        notes=notes,
        public_id=parsed_public_id,
        key_hash=hash_api_key(raw_key),
        key_fast_hash=fast_hash_api_key(raw_key),
    )
    return api_key, raw_key


def _record_api_key_usage(api_key, *, now):
    """Avoid a write on every authenticated API request."""
    threshold = now - API_KEY_USAGE_WRITE_INTERVAL
    if api_key.last_used_at is not None and api_key.last_used_at > threshold:
        return
    ClientApiKey.objects.filter(pk=api_key.pk).filter(
        Q(last_used_at__isnull=True) | Q(last_used_at__lte=threshold)
    ).update(last_used_at=now, updated_at=now)


def authenticate_bearer_token(authorization_header):
    if not authorization_header:
        return AuthResult(client=None, error="missing_authorization")

    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return AuthResult(client=None, error="invalid_authorization")

    raw_api_key = token.strip()
    if raw_api_key.startswith("relay_admin_"):
        return AuthResult(client=None, error="invalid_api_key")
    public_id = public_id_from_raw_key(raw_api_key)
    api_keys = ClientApiKey.objects.select_related("client", "client__organization").filter(revoked_at__isnull=True)
    if public_id:
        api_keys = api_keys.filter(public_id=public_id)
    else:
        # Compatibility for explicitly provisioned exact tokens that predate
        # the public-id format. Resolve them by the fast verifier only: an
        # unknown prefixless token never scans Python rows or runs PBKDF2.
        api_keys = api_keys.filter(key_fast_hash=fast_hash_api_key(raw_api_key))

    for api_key in api_keys:
        if check_api_key_fast(raw_api_key, api_key.key_fast_hash):
            if not api_key.client.is_active:
                return AuthResult(client=None, error="inactive_client")
            _record_api_key_usage(api_key, now=timezone.now())
            return AuthResult(client=api_key.client)
        if public_id and not api_key.key_fast_hash and check_api_key(raw_api_key, api_key.key_hash):
            # Legacy PBKDF2-only key (pre-fast-hash, transfer import, or a
            # rollback): upgrade to the fast verifier on the same write that
            # records usage, so only this request pays PBKDF2.
            if not api_key.client.is_active:
                return AuthResult(client=None, error="inactive_client")
            now = timezone.now()
            ClientApiKey.objects.filter(pk=api_key.pk, key_fast_hash="").update(
                key_fast_hash=fast_hash_api_key(raw_api_key),
                last_used_at=now,
                updated_at=now,
            )
            return AuthResult(client=api_key.client)

    return AuthResult(client=None, error="invalid_api_key")
