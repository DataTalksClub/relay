"""HTTPS-only outbound fetches pinned to public DNS results.

User-controlled URLs must not be able to reach private services or move to a
different address through redirects or a second DNS lookup at connect time.
"""

import http.client
import ipaddress
import socket
import urllib.request
from urllib.parse import urlsplit


class UnsafeFetchURL(ValueError):
    """A URL cannot be fetched safely; ``code`` is safe to persist in job logs."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, addresses, **kwargs):
        self._addresses = addresses
        super().__init__(host, **kwargs)

    def connect(self):
        last_error = None
        for address in self._addresses:
            raw_socket = None
            try:
                raw_socket = socket.create_connection(
                    (address, self.port), self.timeout, self.source_address
                )
                self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
                return
            except OSError as exc:
                last_error = exc
                if raw_socket is not None:
                    raw_socket.close()

        if last_error is not None:
            raise last_error
        raise OSError("no public address available")


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, *, addresses):
        self._addresses = addresses
        super().__init__()

    def https_open(self, request):
        return self.do_open(
            lambda host, **kwargs: _PinnedHTTPSConnection(
                host, addresses=self._addresses, **kwargs
            ),
            request,
            context=self._context,
            check_hostname=self._check_hostname,
        )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request_url(url_or_request):
    if isinstance(url_or_request, urllib.request.Request):
        return url_or_request.full_url
    return url_or_request


def _public_addresses(host, port):
    try:
        literal = ipaddress.ip_address(host)
        records = [(None, str(literal))]
    except ValueError:
        try:
            results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeFetchURL("dns_resolution_failed") from exc
        records = [(family, sockaddr[0]) for family, _kind, _proto, _canonname, sockaddr in results]

    addresses = []
    for _family, address in records:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeFetchURL("invalid_resolved_address") from exc
        if not ip.is_global:
            raise UnsafeFetchURL("non_public_address")
        normalized = str(ip)
        if normalized not in addresses:
            addresses.append(normalized)

    if not addresses:
        raise UnsafeFetchURL("dns_resolution_empty")
    return tuple(addresses)


def safe_urlopen(url_or_request, *, timeout):
    """Open an HTTPS URL only when every resolved address is globally routable.

    The socket connects to one of the checked IP addresses directly while TLS
    still verifies the URL hostname. Redirects and configured HTTP proxies are
    disabled so neither can bypass the address check.
    """
    url = _request_url(url_or_request)
    if not isinstance(url, str):
        raise UnsafeFetchURL("invalid_url")
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() != "https":
            raise UnsafeFetchURL("https_required")
        if parsed.username is not None or parsed.password is not None or not parsed.hostname:
            raise UnsafeFetchURL("invalid_url")
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii")
        port = parsed.port or 443
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, UnsafeFetchURL):
            raise
        raise UnsafeFetchURL("invalid_url") from exc

    if not 1 <= port <= 65535 or not host or "%" in host:
        raise UnsafeFetchURL("invalid_url")
    addresses = _public_addresses(host, port)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
        _PinnedHTTPSHandler(addresses=addresses),
    )
    return opener.open(url_or_request, timeout=timeout)
