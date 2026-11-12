import socket
import urllib.request

import pytest
from datamailer_cli import cli

from conftest import FakeServer

CAMPAIGN_KEY = "cmp-registration-production-ai-agents-generative-media-10ac3865"


@pytest.fixture
def server(monkeypatch):
    fake = FakeServer()
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    return fake


def test_campaign_links_prints_clicks_by_url(server, capsys):
    server.route(
        "GET",
        f"/api/campaigns/{CAMPAIGN_KEY}/links",
        lambda _payload: (
            200,
            {
                "campaign": CAMPAIGN_KEY,
                "links": [
                    {"url": "https://example.com/course", "total_clicks": 18, "unique_clickers": 12}
                ],
            },
        ),
    )

    assert cli.main(
        ["campaigns", "links", CAMPAIGN_KEY, "--url", "https://relay.example.test", "--api-key", "client-key"]
    ) == 0

    assert server.calls == [("GET", f"/api/campaigns/{CAMPAIGN_KEY}/links", None)]
    output = capsys.readouterr().out
    assert "Total clicks" in output
    assert "https://example.com/course\t18\t12" in output


def test_campaign_links_prints_json_when_requested(server, capsys):
    server.route(
        "GET",
        f"/api/campaigns/{CAMPAIGN_KEY}/links",
        lambda _payload: (200, {"campaign": CAMPAIGN_KEY, "links": []}),
    )

    assert cli.main(
        [
            "campaigns",
            "links",
            CAMPAIGN_KEY,
            "--url",
            "https://relay.example.test",
            "--api-key",
            "client-key",
            "--json",
        ]
    ) == 0
    assert '"links": []' in capsys.readouterr().out


def test_campaign_links_shows_timeout_error(monkeypatch, capsys):
    def timed_out(*_args, **_kwargs):
        raise socket.timeout("read timed out")

    monkeypatch.setattr(urllib.request, "urlopen", timed_out)

    assert cli.main(
        ["campaigns", "links", CAMPAIGN_KEY, "--url", "https://relay.example.test", "--api-key", "client-key"]
    ) == 1
    assert "timed out after 30 seconds" in capsys.readouterr().err
