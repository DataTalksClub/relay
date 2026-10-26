"""Website access-log policy, with Relay's dynamic path values removed.

Keep timestamp, method, path, protocol, status, bytes, microseconds and safe IDs.
Never emit a raw request target, query, address, user, referrer or user agent.
Gunicorn imports this module before Django setup; resolve routes only at runtime.
"""

import re
import uuid

from gunicorn.glogging import Logger

ACCESS_LOG_FORMAT = (
    '%(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s %(D)s '
    'request_id="%({x-request-id}o)s" correlation_id="%({x-correlation-id}o)s"'
)


def safe_id(value):
    """UUIDs cannot smuggle an email, credential or log delimiter into the log."""
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None


def safe_path(path):
    # Route templates retain the useful endpoint while removing contact emails,
    # recipient tokens, external keys and arbitrary values on 404 requests.
    from django.urls import Resolver404, resolve  # noqa: PLC0415

    try:
        route = resolve(path).route
    except Resolver404:
        return "/[unmatched]"
    return "/" + re.sub(r"<(?:(?:[^:>]+):)?([^>]+)>", r"[\1]", route).lstrip("/")


class PrivateAccessLogger(Logger):
    def atoms(self, resp, req, environ, request_time):
        values = super().atoms(resp, req, environ, request_time)
        # Return only the reviewed atoms so a future format edit cannot expose
        # Gunicorn's raw request, headers, environment or authenticated user.
        return {
            "t": values["t"],
            "m": values["m"]
            if values["m"] in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
            else "-",
            "U": safe_path(environ.get("PATH_INFO", "")),
            "H": values["H"] if re.fullmatch(r"HTTP/\d(?:\.\d)?", values["H"]) else "-",
            "s": values["s"],
            "b": values["b"],
            "D": values["D"],
            "{x-request-id}o": safe_id(environ.get("HTTP_X_REQUEST_ID")) or str(uuid.uuid4()),
            "{x-correlation-id}o": safe_id(values.get("{x-correlation-id}o")) or "-",
        }
