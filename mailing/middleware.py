import json
import logging
import sys

from django.db import DatabaseError
from django.http import HttpResponse, JsonResponse
from django.views import defaults

from mailing.db_retry import is_transient_db_error

logger = logging.getLogger(__name__)


def _is_database_lock_timeout(exception):
    seen = set()
    current = exception
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        diag = getattr(current, "diag", None)
        sqlstate = getattr(diag, "sqlstate", None) or getattr(current, "pgcode", None)
        if sqlstate == "55P03" or "lock timeout" in str(current).lower():
            return True
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return False


def database_error_response(request, exception, *, force_json=False):
    request._relay_error_class = type(exception).__name__
    if _is_database_lock_timeout(exception):
        status = 409
        code = "database_lock_timeout"
        message = "The request could not acquire a database lock. Please retry."
    elif is_transient_db_error(exception):
        status = 503
        code = "service_unavailable"
        message = "Database is temporarily unavailable. Please retry later."
    else:
        status = 500
        code = "database_error"
        message = "The request failed while accessing the database."

    path = getattr(request, "path", "")
    accept = request.headers.get("Accept", "") if hasattr(request, "headers") else ""
    if force_json or path.startswith("/api/") or "application/json" in accept:
        response = JsonResponse(
            {
                "error": {
                    "code": code,
                    "message": message,
                }
            },
            status=status,
        )
    else:
        title = "Service Unavailable (503)" if status == 503 else f"Database error ({status})"
        response = HttpResponse(
            f"<!doctype html><html lang=\"en\"><head><title>{title}</title></head>"
            f"<body><h1>{title}</h1><p>{message}</p></body></html>",
            status=status,
            content_type="text/html; charset=utf-8",
        )
    if status == 503:
        response["Retry-After"] = "30"
        response["X-Relay-503-Reason"] = "database_unavailable"
    elif status == 500:
        response["X-Relay-Error-Reason"] = "database_error"
    else:
        response["X-Relay-Error-Reason"] = "database_lock_timeout"
    return response


def _log_5xx_response(request, response):
    if response.status_code == 503 and not response.has_header("X-Relay-503-Reason"):
        response["X-Relay-503-Reason"] = "application_unavailable"
    if response.status_code >= 500:
        logger.error(
            json.dumps(
                {
                    "event": "http_5xx",
                    "method": getattr(request, "method", ""),
                    "path": getattr(request, "path", ""),
                    "status": response.status_code,
                    "error_class": getattr(request, "_relay_error_class", None),
                    "reason": response.get("X-Relay-503-Reason")
                    or response.get("X-Relay-Error-Reason")
                    or "unhandled_5xx",
                },
                separators=(",", ":"),
            )
        )
    return response


def handle_server_error(request):
    """Return the correctly classified response for an uncaught DB error."""
    exc_type, exc_value, _ = sys.exc_info()
    if exc_value is not None and isinstance(exc_value, DatabaseError):
        return database_error_response(request, exc_value)
    return defaults.server_error(request)



class DatabaseOutageMiddleware:
    """Translate unhandled database errors during outages into clean 503 responses."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            response = self.get_response(request)
        except DatabaseError as exc:
            response = database_error_response(request, exc)
        return _log_5xx_response(request, response)

    def process_exception(self, request, exception):
        if isinstance(exception, DatabaseError):
            return database_error_response(request, exception)
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

