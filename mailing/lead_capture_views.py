"""Public lead capture pages, served at materials.dtcdev.click (Relay path /m/).

No login, no API key. CSRF is exempt because the pages are reached through a
second CloudFront hostname and carry no session: the double opt-in email is the
proof of address ownership, the honeypot and rate limits keep scripts out.
"""

import functools
import logging
from pathlib import Path

from django.conf import settings
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from mailing.services import lead_capture
from mailing.services.lead_capture import LeadError

logger = logging.getLogger(__name__)

HONEYPOT_FIELD = "company"

MESSAGES = {
    "email_required": "Please enter your email address.",
    "email_invalid": "That email address doesn't look right.",
    "consent_required": "Please tick the box so I can send you the materials.",
    "rate_limited": "Too many attempts from here. Please try again in a few minutes.",
    "closed": "Signups for this page are closed.",
    "not_ready": "This page is not set up yet. Please try again later.",
    "invalid_token": "This link is not valid. Please sign up again on the page.",
    "expired_token": "This link has expired. Please sign up again on the page.",
    "not_confirmed": "Please confirm your email first, using the link in the email.",
    "no_material": "There is nothing to download for this page yet.",
}


def _client_ip(request):
    # Leftmost X-Forwarded-For entry: the visitor as seen by the first
    # CloudFront hop. Best effort (a client can prepend entries); it only feeds
    # the per-IP rate limit and the consent evidence.
    forwarded = [part.strip() for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if part.strip()]
    return (forwarded[0] if forwarded else request.META.get("REMOTE_ADDR", ""))[:64]


@functools.cache
def _tokens_css():
    # The pages are served from materials.dtcdev.click, where /static/ is not
    # routed, so dakit's colour/space roles are inlined (about 5 KB).
    path = Path(settings.BASE_DIR) / "static" / "dakit" / "tokens.css"
    return path.read_text() if path.is_file() else ""


def _render(request, template, context, status=200):
    response = render(request, f"leads/{template}", {"tokens_css": _tokens_css(), **context}, status=status)
    response["X-Robots-Tag"] = "noindex"
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _message(request, event, code, status=None):
    return _render(
        request,
        "message.html",
        {"event": event, "message": MESSAGES.get(code, "Something went wrong."), "back_url": _page_url(event)},
        status=status or 400,
    )


def _page_url(event):
    return lead_capture.event_url(event) if event else ""


def _event_or_404(slug):
    try:
        return lead_capture.active_event(slug)
    except LeadError as exc:
        raise Http404("No such page") from exc


def index(request):
    return _render(request, "message.html", {"event": None, "message": "Nothing here. Scan the QR code from the talk."}, 404)


@csrf_exempt
@require_http_methods(["GET", "POST", "HEAD"])
def event_page(request, slug):
    event = _event_or_404(slug)
    context = {"event": event, "form": {"email": "", "name": "", "consent": False}, "error": ""}
    if request.method != "POST":
        return _render(request, "page.html", context)

    form = {
        "email": request.POST.get("email", "")[:320],
        "name": request.POST.get("name", "")[:200],
        "consent": request.POST.get("consent") in {"yes", "on", "true", "1"},
    }
    if request.POST.get(HONEYPOT_FIELD):
        logger.info("relay_lead_honeypot event=%s", event.slug)
        return _render(request, "sent.html", {"event": event, "page_url": _page_url(event)})
    try:
        status = lead_capture.request_lead(
            event,
            email=form["email"],
            name=form["name"],
            consent=form["consent"],
            evidence={
                "ip": _client_ip(request),
                "user_agent": request.headers.get("User-Agent", ""),
                "referer": request.headers.get("Referer", ""),
            },
        )
    except LeadError as exc:
        if exc.status >= 500:
            logger.error("relay_lead_signup_failed event=%s code=%s", event.slug, exc.code)
        return _render(
            request,
            "page.html",
            {"event": event, "form": form, "error": MESSAGES.get(exc.code, "Something went wrong.")},
            status=exc.status,
        )
    logger.info("relay_lead_signup event=%s status=%s", event.slug, status)
    return _render(request, "sent.html", {"event": event, "page_url": _page_url(event)})


@csrf_exempt
@require_http_methods(["GET", "POST", "HEAD"])
def confirm(request, slug):
    """GET shows a confirm button; only the POST confirms (mail scanners prefetch GET links)."""
    event = _event_or_404(slug)
    token = request.POST.get("t") if request.method == "POST" else request.GET.get("t")
    if request.method != "POST":
        try:
            lead_capture.read_token(token, event)
        except LeadError as exc:
            return _message(request, event, exc.code)
        return _render(request, "confirm.html", {"event": event, "token": token})
    try:
        lead_capture.confirm_lead(event, token, ip=_client_ip(request))
    except LeadError as exc:
        return _message(request, event, exc.code, exc.status)
    logger.info("relay_lead_confirmed event=%s", event.slug)
    return _render(
        request,
        "confirmed.html",
        {"event": event, "download_url": f"{lead_capture.event_url(event, '/download')}?t={token}"},
    )


@require_http_methods(["GET", "HEAD"])
def download(request, slug):
    event = _event_or_404(slug)
    try:
        material = lead_capture.material_for_download(event, request.GET.get("t"))
    except LeadError as exc:
        return _message(request, event, exc.code, exc.status)
    if material is None:
        return HttpResponseRedirect(event.material_url)
    response = HttpResponse(bytes(material.data), content_type=material.content_type)
    filename = material.filename.replace('"', "")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Robots-Tag"] = "noindex"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_http_methods(["GET", "HEAD"])
def qr(request, slug, kind):
    if kind not in {"svg", "png"}:
        raise Http404("Unknown format")
    event = _event_or_404(slug)
    data = lead_capture.qr_code(lead_capture.event_url(event), kind=kind)
    content_type = "image/png" if kind == "png" else "image/svg+xml"
    response = HttpResponse(data, content_type=content_type)
    if request.GET.get("download"):
        response["Content-Disposition"] = f'attachment; filename="{event.slug}-qr.{kind}"'
    response["Cache-Control"] = "public, max-age=300"
    return response
