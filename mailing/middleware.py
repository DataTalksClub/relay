import logging
import sys

from django.db import DatabaseError
from django.http import HttpResponse, JsonResponse
from django.views import defaults

logger = logging.getLogger(__name__)


def _db_error_response(request):
    path = getattr(request, "path", "")
    accept = request.headers.get("Accept", "") if hasattr(request, "headers") else ""
    if path.startswith("/api/") or accept == "application/json":
        response = JsonResponse(
            {
                "error": {
                    "code": "service_unavailable",
                    "message": "Database is temporarily unavailable. Please retry later.",
                }
            },
            status=503,
        )
        response["Retry-After"] = "30"
        return response
    response = HttpResponse(
        "<!doctype html><html lang=\"en\"><head><title>Service Unavailable (503)</title></head>"
        "<body><h1>Service Unavailable (503)</h1><p>Database is temporarily unavailable. Please try again later.</p></body></html>",
        status=503,
        content_type="text/html; charset=utf-8",
    )
    response["Retry-After"] = "30"
    return response


def handle_server_error(request):
    """Custom 500 handler that returns 503 for DatabaseError outages."""
    exc_type, exc_value, _ = sys.exc_info()
    if exc_value is not None and isinstance(exc_value, DatabaseError):
        logger.warning("Database unavailable handling %s: %s", getattr(request, "path", "/"), exc_value)
        return _db_error_response(request)
    return defaults.server_error(request)



class DatabaseOutageMiddleware:
    """Translate unhandled database errors during outages into clean 503 responses."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        except DatabaseError as exc:
            logger.warning("Database unavailable handling %s: %s", request.path, exc)
            return _db_error_response(request)

    def process_exception(self, request, exception):
        if isinstance(exception, DatabaseError):
            logger.warning("Database unavailable handling %s: %s", request.path, exception)
            return _db_error_response(request)
        return None


class DropContentLengthOnNoContent:
    """Remove Content-Length from 204 responses.

    CommonMiddleware adds the header after the view. HTTP/2 rejects it on
    204, which breaks the public signup CORS preflight.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.status_code == 204 and response.has_header("Content-Length"):
            del response["Content-Length"]
        return response

