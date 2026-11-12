"""Shared Cognito login adapter for Relay's Django staff session."""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import urllib.parse
import urllib.request

import jwt
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model, login, logout
from django.http import HttpResponse, HttpResponseRedirect
from django.template.loader import render_to_string
from django.views.decorators.http import require_GET, require_POST

logger = logging.getLogger(__name__)

_PROVIDER_ERROR_CODES = frozenset(
    {
        "access_denied",
        "invalid_client",
        "invalid_grant",
        "invalid_request",
        "invalid_scope",
        "login_required",
        "server_error",
        "temporarily_unavailable",
        "unauthorized_client",
        "unsupported_response_type",
    }
)

_AUTH_ERROR_MESSAGES = {
    "provider": "The sign-in provider could not complete authentication. Try again or contact Relay support. No account changes were made.",
    "session": "This sign-in session expired or was interrupted. Start a fresh sign-in. No account changes were made.",
    "verification": "Relay could not verify the sign-in response. Contact Relay support. No account changes were made.",
    "workspace": "Sign in with a verified Google Workspace account ending in @datatalks.club. No account changes were made.",
}

_DEFAULT_AUTH_ERROR_MESSAGE = "Authentication could not be completed. No account changes were made."

# The operator dashboard at "/" is the canonical post-login landing page. A
# login must only reach /admin/ when the operator deliberately asked for it
# (a safe requested return_to), never as a side effect of signing in.
_DEFAULT_RETURN_TO = "/"


def configured():
    return all(
        getattr(settings, key, "")
        for key in ("AUTH_BASE_URL", "AUTH_CLIENT_ID", "AUTH_CALLBACK_URL", "AUTH_ISSUER", "AUTH_JWKS_URL")
    )


def _b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _safe_return_to(value):
    if not isinstance(value, str) or not value:
        return _DEFAULT_RETURN_TO
    candidate = value
    for _ in range(8):
        decoded = urllib.parse.unquote(candidate)
        if decoded == candidate:
            break
        candidate = decoded
    controls = any(ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\u2028\u2029" for char in candidate)
    malformed_escape = any(
        candidate[index] == "%"
        and (
            index + 2 >= len(candidate)
            or any(char not in "0123456789abcdefABCDEF" for char in candidate[index + 1 : index + 3])
        )
        for index in range(len(candidate))
    )
    if controls or malformed_escape or "\\" in candidate or candidate.startswith("//"):
        return _DEFAULT_RETURN_TO
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return _DEFAULT_RETURN_TO
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return _DEFAULT_RETURN_TO
    if parsed.path == "/login" or parsed.path == "/logout" or parsed.path.startswith("/auth/"):
        return _DEFAULT_RETURN_TO
    return urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))


def _security_headers(response):
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _error_redirect(reason=""):
    if reason not in _AUTH_ERROR_MESSAGES:
        reason = ""
    location = "/auth/error"
    if reason:
        location = f"{location}?{urllib.parse.urlencode({'reason': reason})}"
    response = HttpResponseRedirect(location)
    response.status_code = 303
    return _security_headers(response)


@require_GET
def begin(request):
    if not configured():
        return HttpResponse("Shared authentication is not configured", status=503)
    state = _b64url(secrets.token_bytes(32))
    verifier = _b64url(secrets.token_bytes(48))
    nonce = _b64url(secrets.token_bytes(32))
    request.session["oidc"] = {
        "state": state,
        "verifier": verifier,
        "nonce": nonce,
        "return_to": _safe_return_to(request.GET.get("return_to")),
    }
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": settings.AUTH_CLIENT_ID,
            "redirect_uri": settings.AUTH_CALLBACK_URL,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
        }
    )
    return _security_headers(HttpResponseRedirect(f"{settings.AUTH_BASE_URL}/oauth2/authorize?{query}"))


def _exchange(code, verifier):
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": settings.AUTH_CLIENT_ID,
            "code": code,
            "redirect_uri": settings.AUTH_CALLBACK_URL,
            "code_verifier": verifier,
        }
    ).encode()
    request = urllib.request.Request(
        f"{settings.AUTH_BASE_URL}/oauth2/token",
        data=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def _verify(id_token):
    key = jwt.PyJWKClient(settings.AUTH_JWKS_URL, timeout=15).get_signing_key_from_jwt(id_token)
    return jwt.decode(
        id_token,
        key.key,
        algorithms=["RS256"],
        audience=settings.AUTH_CLIENT_ID,
        issuer=settings.AUTH_ISSUER,
        options={"require": ["exp", "iat", "iss", "aud", "sub"]},
    )


@require_GET
def callback(request):
    pending = request.session.pop("oidc", None)
    code, state = request.GET.get("code", ""), request.GET.get("state", "")
    if not pending or not state or not hmac.compare_digest(state, str(pending.get("state", ""))):
        logger.warning(
            "relay_oidc_callback_rejected reason=session_state pending=%s code_present=%s state_present=%s",
            bool(pending),
            bool(code),
            bool(state),
        )
        return _error_redirect("session")
    provider_error = request.GET.get("error", "")
    if provider_error:
        description = request.GET.get("error_description", "")[:1024].lower()
        workspace_restriction = (
            provider_error == "invalid_request"
            and "verified account" in description
            and "approved workspace domain" in description
        )
        category = "workspace" if workspace_restriction else "provider"
        safe_code = provider_error if provider_error in _PROVIDER_ERROR_CODES else "other"
        logger.warning(
            "relay_oidc_provider_error code=%s category=%s description_present=%s",
            safe_code,
            category,
            bool(description),
        )
        return _error_redirect(category)
    if not code:
        logger.warning("relay_oidc_callback_rejected reason=missing_code")
        return _error_redirect("provider")
    try:
        claims = _verify(_exchange(code, pending["verifier"])["id_token"])
    except Exception as exc:  # provider and JWKS errors can contain credential-bearing details
        logger.error("relay_oidc_token_verification_failed exception_type=%s", type(exc).__name__)
        return _error_redirect("provider")
    if not hmac.compare_digest(str(claims.get("nonce", "")), str(pending.get("nonce", ""))):
        logger.warning("relay_oidc_callback_rejected reason=nonce_mismatch")
        return _error_redirect("verification")
    email = claims.get("email")
    if not isinstance(email, str) or claims.get("email_verified") is not True:
        logger.warning(
            "relay_oidc_callback_rejected reason=unverified_email_claim email_present=%s email_verified=%s",
            isinstance(email, str) and bool(email),
            claims.get("email_verified") is True,
        )
        return _error_redirect("verification")
    email = email.lower()
    model = get_user_model()
    user = model.objects.filter(email__iexact=email).first()
    if user is None:
        user = model.objects.create_user(username=email, email=email)
    if not user.is_active:
        logger.warning("relay_oidc_callback_rejected reason=inactive_user")
        return _error_redirect("verification")
    changed = []
    if user.email != email:
        user.email = email
        changed.append("email")
    if not user.is_staff:
        user.is_staff = True
        changed.append("is_staff")
    if changed:
        user.save(update_fields=changed)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return _security_headers(HttpResponseRedirect(_safe_return_to(pending.get("return_to"))))


@require_GET
def auth_error(request):
    reason = request.GET.get("reason", "")
    message = _AUTH_ERROR_MESSAGES.get(reason, _DEFAULT_AUTH_ERROR_MESSAGE)
    response = HttpResponse(
        render_to_string("auth_error.html", {"message": message}),
        status=403,
        content_type="text/html; charset=utf-8",
    )
    # Only the page's own static assets: the dakit bundle, its fonts and the
    # theme boot script.
    response["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'self'; font-src 'self'; script-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return _security_headers(response)


@require_POST
def end(request):
    """Sign out. POST only, behind Django's CSRF check: a GET (a link, an
    image, a cross-site navigation) must never end the session."""
    logout(request)
    if not configured() or not settings.AUTH_LOGOUT_URL:
        return HttpResponseRedirect("/admin/login/")
    query = urllib.parse.urlencode({"client_id": settings.AUTH_CLIENT_ID, "logout_uri": settings.AUTH_LOGOUT_URL})
    return HttpResponseRedirect(f"{settings.AUTH_BASE_URL}/logout?{query}")


def admin_login(request):
    if configured() and request.GET.get("local") != "1":
        # staff_member_required hands unauthenticated visitors here with
        # next=<requested page>; carry a safe next through shared auth so a
        # deliberate /admin/ visit still lands in the admin, while a login
        # that never asked for a page falls back to the operator dashboard.
        return_to = _safe_return_to(request.GET.get("next"))
        return HttpResponseRedirect(f"/auth/login?{urllib.parse.urlencode({'return_to': return_to})}")
    return admin.site.login(request)
