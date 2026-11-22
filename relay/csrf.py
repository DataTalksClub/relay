"""CSRF failure page: log why the check failed and tell the user what to do."""

import logging

from django.shortcuts import render

logger = logging.getLogger("mailing.csrf")


def csrf_failure(request, reason=""):
    logger.warning(
        "relay_csrf_failure path=%s reason=%r origin=%r referer_present=%s "
        "cookie_present=%s host=%s secure=%s",
        request.path,
        reason,
        request.META.get("HTTP_ORIGIN"),
        bool(request.META.get("HTTP_REFERER")),
        "csrftoken" in request.COOKIES,
        request.get_host(),
        request.is_secure(),
    )
    response = render(request, "csrf_failure.html", {"next_url": request.META.get("HTTP_REFERER", "")}, status=403)
    response["Cache-Control"] = "no-store"
    return response
