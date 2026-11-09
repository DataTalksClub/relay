import socket
import urllib.request

import pytest
from datamailer_cli import config
from datamailer_cli.api import ApiError, DatamailerClient


def test_timeout_fails_with_a_clear_error(monkeypatch):
    def timed_out(*_args, **_kwargs):
        raise socket.timeout("read timed out")

    monkeypatch.setattr(urllib.request, "urlopen", timed_out)
    client = DatamailerClient("https://relay.example.test", "relay_admin_test", timeout=2)
    with pytest.raises(ApiError, match="timed out after 2 seconds"):
        client.request("GET", "/api/admin/dashboard")


def test_relay_environment_names_take_precedence(monkeypatch, tmp_path):
    path = tmp_path / "relay.toml"
    config.save_file({"url": "https://file.test", "api_key": "file"}, path)
    monkeypatch.setenv("RELAY_URL", "https://env.test")
    monkeypatch.setenv("DATAMAILER_URL", "https://legacy.test")
    monkeypatch.setenv("RELAY_API_KEY", "relay-key")
    settings = config.resolve(object(), path)
    assert settings.base_url == "https://env.test"
    assert settings.api_key == "relay-key"
