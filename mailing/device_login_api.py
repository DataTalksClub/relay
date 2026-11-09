"""Relay endpoints and approval page for DataTalksClub's shared device flow."""

from __future__ import annotations

import hashlib
import json
import secrets
import urllib.parse
from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from mailing.models import DeviceLogin
from mailing.services.admin_auth import authenticate_admin_request, create_admin_api_key
from mailing.services.operator_management import audit

USER_CODE_ALPHABET = "BCDFGHJKMNPQRSTWXZ23456789"
DEVICE_LOGIN_SECONDS = 600
POLL_INTERVAL_SECONDS = 5


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _client_ip(request):
    return request.META.get("REMOTE_ADDR") or None


def _rate_limited(request, purpose: str, limit: int) -> bool:
    key = f"relay-device:{purpose}:{_hash(_client_ip(request) or 'unknown')}"
    if cache.add(key, 1, timeout=300):
        return False
    try:
        return cache.incr(key) > limit
    except ValueError:
        cache.set(key, 1, timeout=300)
        return False


def _no_store(response):
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _json_body(request):
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


@csrf_exempt
@require_POST
def device_start(request):
    if _rate_limited(request, "start", 10):
        return _no_store(JsonResponse({"error": "slow_down"}, status=429))
    body = _json_body(request)
    if body is None:
        return _no_store(JsonResponse({"error": "invalid_request"}, status=400))
    DeviceLogin.objects.filter(expires_at__lt=timezone.now() - timedelta(days=1)).filter(
        Q(admin_api_key__isnull=True) | Q(admin_api_key__revoked_at__isnull=False)
    ).delete()
    label = str(body.get("label", ""))[:160]
    device_code = secrets.token_urlsafe(32)
    for _attempt in range(5):
        user_code_raw = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))
        user_code = f"{user_code_raw[:4]}-{user_code_raw[4:]}"
        try:
            DeviceLogin.objects.create(
                device_code_hash=_hash(device_code),
                user_code_hash=_hash(user_code),
                label=label,
                request_ip=_client_ip(request),
                expires_at=timezone.now() + timedelta(seconds=DEVICE_LOGIN_SECONDS),
            )
            break
        except IntegrityError:
            # A rare user-code collision retries. Infrastructure failures are
            # surfaced immediately instead of spinning in the request worker.
            continue
    else:
        return _no_store(JsonResponse({"error": "server_error"}, status=503))
    base = request.build_absolute_uri("/").rstrip("/")
    verify_url = f"{base}/device?{urllib.parse.urlencode({'code': user_code})}"
    return _no_store(JsonResponse({
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": f"{base}/device",
        "verification_uri_complete": verify_url,
        "expires_in": DEVICE_LOGIN_SECONDS,
        "interval": POLL_INTERVAL_SECONDS,
    }))


@csrf_exempt
@require_POST
def device_token(request):
    if _rate_limited(request, "poll", 120):
        return _no_store(JsonResponse({"error": "slow_down"}, status=200))
    body = _json_body(request)
    device_code = str(body.get("device_code", "")) if body else ""
    if not device_code or body.get("grant_type") != "urn:ietf:params:oauth:grant-type:device_code":
        return _no_store(JsonResponse({"error": "invalid_request"}, status=400))
    with transaction.atomic():
        login = DeviceLogin.objects.select_for_update().filter(device_code_hash=_hash(device_code)).first()
        if login is None:
            return _no_store(JsonResponse({"error": "expired_token"}, status=200))
        if login.expires_at <= timezone.now():
            return _no_store(JsonResponse({"error": "expired_token"}, status=200))
        if login.status == "pending":
            return _no_store(JsonResponse({"error": "authorization_pending"}, status=200))
        if login.status == "denied":
            return _no_store(JsonResponse({"error": "access_denied"}, status=200))
        if login.status != "approved" or not login.user_id:
            return _no_store(JsonResponse({"error": "expired_token"}, status=200))
        try:
            key, token = create_admin_api_key(user=login.user, name=f"Relay CLI device {login.pk}")
        except Exception:
            return _no_store(JsonResponse({"error": "server_error"}, status=503))
        login.status = "consumed"
        login.admin_api_key = key
        login.save(update_fields=["status", "admin_api_key"])
        audit(login.user, "admin.api_key.create", key, {"source": "device_login", "device_login_id": login.pk})
    return _no_store(JsonResponse({
        "token": token,
        "subject": str(login.user_id),
        "email": login.user.email,
        "expires_at": 0,
    }))


@csrf_exempt
@require_POST
def device_revoke(request):
    key = authenticate_admin_request(request)
    if key is None:
        return _no_store(JsonResponse({"error": "invalid_token"}, status=401))
    if not DeviceLogin.objects.filter(admin_api_key=key).exists():
        return _no_store(JsonResponse({"error": "not_a_device_session"}, status=403))
    key.revoked_at = timezone.now()
    key.save(update_fields=["revoked_at", "updated_at"])
    return _no_store(JsonResponse({"revoked": True}))


def _user_code(raw):
    return "".join(character for character in str(raw or "").upper() if character.isalnum())


def _login_for_code(raw):
    normalized = _user_code(raw)
    if len(normalized) != 8:
        return None
    canonical = f"{normalized[:4]}-{normalized[4:]}"
    return DeviceLogin.objects.filter(user_code_hash=_hash(canonical)).first()


def _approval_context(request, login):
    return {
        "login": login,
        "user_code": request.GET.get("code") or request.POST.get("code", ""),
        "expired": not login or login.expires_at <= timezone.now() or login.status != "pending",
    }


@staff_member_required
@require_GET
def device_approval(request):
    if _rate_limited(request, "review", 60):
        return _no_store(HttpResponse("Too many attempts. Wait a few minutes and try again.", status=429))
    login = _login_for_code(request.GET.get("code")) if request.GET.get("code") else None
    response = render(request, "mailing/operator/device_login.html", _approval_context(request, login))
    return _no_store(response)


@staff_member_required
@require_POST
def device_approval_decide(request):
    if _rate_limited(request, "decision", 20):
        return _no_store(HttpResponse("Too many attempts. Wait a few minutes and try again.", status=429))
    code = request.POST.get("code", "")
    approve_value = request.POST.get("approve")
    login = _login_for_code(code)
    if approve_value not in {"true", "false"} or not login or login.expires_at <= timezone.now() or login.status != "pending":
        response = render(request, "mailing/operator/device_login.html", {
            "login": login,
            "user_code": code,
            "expired": True,
            "error": "That device code is unknown, expired, or already decided.",
        }, status=400)
        return _no_store(response)
    login.status = "approved" if approve_value == "true" else "denied"
    login.user = request.user if approve_value == "true" else None
    login.decided_at = timezone.now()
    login.save(update_fields=["status", "user", "decided_at"])
    audit(request.user, "device_login.approve" if approve_value == "true" else "device_login.deny", login)
    response = render(request, "mailing/operator/device_login.html", {
        "login": login,
        "user_code": code,
        "decided": True,
        "approved": approve_value == "true",
    })
    return _no_store(response)
