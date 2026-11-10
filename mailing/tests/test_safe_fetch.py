import socket
import urllib.request

import pytest

from relay import safe_fetch


def _resolved_records(*addresses):
    records = []
    for address in addresses:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
        records.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
    return records


@pytest.mark.parametrize("address", ["10.1.2.3", "127.0.0.1", "169.254.169.254", "::1", "fe80::1"])
def test_safe_urlopen_rejects_non_public_ip_literals_before_open(monkeypatch, address):
    def fail_if_open(*_args, **_kwargs):
        raise AssertionError("unsafe destination must not be opened")

    monkeypatch.setattr(safe_fetch.urllib.request, "build_opener", fail_if_open)
    url = f"https://[{address}]/resource" if ":" in address else f"https://{address}/resource"

    with pytest.raises(safe_fetch.UnsafeFetchURL, match="non_public_address"):
        safe_fetch.safe_urlopen(url, timeout=1)


def test_safe_urlopen_rejects_mixed_public_and_private_dns_answers(monkeypatch):
    monkeypatch.setattr(
        safe_fetch.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _resolved_records("8.8.8.8", "10.0.0.4"),
    )

    with pytest.raises(safe_fetch.UnsafeFetchURL, match="non_public_address"):
        safe_fetch.safe_urlopen("https://example.com/resource", timeout=1)


def test_safe_urlopen_uses_checked_address_and_no_redirect_handler(monkeypatch):
    monkeypatch.setattr(
        safe_fetch.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: _resolved_records("8.8.8.8"),
    )
    captured = {}

    class FakeOpener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return "response"

    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return FakeOpener()

    monkeypatch.setattr(safe_fetch.urllib.request, "build_opener", fake_build_opener)

    response = safe_fetch.safe_urlopen("https://example.com/resource", timeout=7)

    assert response == "response"
    assert captured["timeout"] == 7
    assert isinstance(captured["handlers"][0], urllib.request.ProxyHandler)
    assert captured["handlers"][0].proxies == {}
    no_redirect = next(
        handler for handler in captured["handlers"] if isinstance(handler, safe_fetch._NoRedirectHandler)
    )
    assert no_redirect.redirect_request(None, None, 302, "Found", {}, "https://other.example/") is None
    pinned = next(
        handler for handler in captured["handlers"] if isinstance(handler, safe_fetch._PinnedHTTPSHandler)
    )
    assert pinned._addresses == ("8.8.8.8",)


def test_pinned_connection_connects_to_resolved_ip_and_keeps_hostname_for_tls(monkeypatch):
    captured = {}
    raw_socket = object()

    class FakeTLSContext:
        def wrap_socket(self, sock, *, server_hostname):
            captured["wrapped"] = sock
            captured["server_hostname"] = server_hostname
            return "tls-socket"

    def fake_create_connection(address, timeout, source_address):
        captured["address"] = address
        captured["timeout"] = timeout
        captured["source_address"] = source_address
        return raw_socket

    monkeypatch.setattr(safe_fetch.socket, "create_connection", fake_create_connection)
    connection = safe_fetch._PinnedHTTPSConnection(
        "example.com",
        port=443,
        addresses=("8.8.8.8",),
        timeout=3,
        context=FakeTLSContext(),
    )

    connection.connect()

    assert captured["address"] == ("8.8.8.8", 443)
    assert captured["wrapped"] is raw_socket
    assert captured["server_hostname"] == "example.com"
    assert connection.sock == "tls-socket"


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://8.8.8.8/resource", "https_required"),
        ("https://user:pass@example.com/resource", "invalid_url"),
    ],
)
def test_safe_urlopen_rejects_disallowed_url_forms_before_dns(monkeypatch, url, code):
    def fail_if_resolved(*_args, **_kwargs):
        raise AssertionError("invalid URL must not trigger DNS")

    monkeypatch.setattr(safe_fetch.socket, "getaddrinfo", fail_if_resolved)

    with pytest.raises(safe_fetch.UnsafeFetchURL, match=code):
        safe_fetch.safe_urlopen(url, timeout=1)
