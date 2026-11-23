from types import SimpleNamespace

from datamailer_cli import auth, cli, config


def test_device_login_uses_shared_flow_and_saves_token(monkeypatch, tmp_path):
    calls = {}

    def device_login(base_url, poster, **kwargs):
        calls.update(base_url=base_url, poster=poster, **kwargs)
        return {"token": "relay_admin_private"}

    monkeypatch.setattr(auth, "_shared_device_login", lambda: SimpleNamespace(device_login=device_login))
    auth.login("https://relay.example.test/", timeout=42, sleeper=lambda _seconds: None)

    saved = config.load_file()
    assert saved == {"url": "https://relay.example.test", "api_key": "relay_admin_private"}
    assert calls["start_path"] == "/api/agent/device/start"
    assert calls["token_path"] == "/api/agent/device/token"
    assert calls["timeout"] == 42


def test_auth_logout_removes_saved_token(monkeypatch):
    config.save_file({"url": "https://relay.example.test", "api_key": "relay_admin_private"})
    calls = []
    def revoke(*args, **kwargs):
        calls.append((args, kwargs))
        return 200, {"revoked": True}

    monkeypatch.setattr(auth, "_post_json", revoke)
    assert auth.logout() is True
    assert config.load_file() == {"url": "https://relay.example.test"}
    assert calls[0][0][0] == "https://relay.example.test/api/agent/device/revoke"
    assert calls[0][1]["bearer"] == "relay_admin_private"


def test_auth_status_does_not_print_token(capsys):
    config.save_file({"url": "https://relay.example.test", "api_key": "private-token"})
    assert cli.cmd_auth_status(SimpleNamespace()) == 0
    output = capsys.readouterr().out
    assert "configured" in output
    assert "private-token" not in output


def test_auth_status_reports_expired_or_rejected(monkeypatch, capsys):
    config.save_file({"url": "https://relay.example.test", "api_key": "relay_admin_deadbeef_secret"})

    def fake_whoami(self):
        raise cli.ApiError("Authentication credentials were not accepted.", status=401)

    monkeypatch.setattr(cli.DatamailerClient, "whoami", fake_whoami)
    assert cli.cmd_auth_status(SimpleNamespace()) == 1
    output = capsys.readouterr().out
    assert "expired/rejected" in output
    assert "relay_admin_deadbeef_secret" not in output


def test_auth_status_reports_valid_operator(monkeypatch, capsys):
    config.save_file({"url": "https://relay.example.test", "api_key": "relay_admin_abc_secret"})
    monkeypatch.setattr(
        cli.DatamailerClient,
        "whoami",
        lambda self: {"user": {"email": "orin@datatalks.club"}, "key": {"name": "box", "prefix": "relay_admin_abc"}},
    )
    assert cli.cmd_auth_status(SimpleNamespace()) == 0
    output = capsys.readouterr().out
    assert "valid (orin@datatalks.club)" in output


def test_whoami_uses_admin_endpoint_for_admin_key(monkeypatch, capsys):
    calls = []

    def fake_request(self, method, path, *, payload=None, query=None):
        calls.append((method, path))
        return 200, {"user": {"email": "orin@datatalks.club"}, "key": {"name": "box", "prefix": "relay_admin_abc"}}

    monkeypatch.setattr(cli.DatamailerClient, "request", fake_request)
    config.save_file({"url": "https://relay.example.test", "api_key": "relay_admin_abc_secret"})
    args = SimpleNamespace(url=None, api_key=None, json=False)
    assert cli.cmd_whoami(args) == 0
    assert calls == [("GET", "/api/admin/whoami")]
    output = capsys.readouterr().out
    assert "orin@datatalks.club" in output


def test_login_timeout_is_reported_without_hanging(monkeypatch):
    class DeviceLoginError(Exception):
        pass

    def device_login(*_args, **_kwargs):
        raise DeviceLoginError("Timed out waiting for approval. Run login again.")

    monkeypatch.setattr(auth, "_shared_device_login", lambda: SimpleNamespace(
        device_login=device_login, DeviceLoginError=DeviceLoginError
    ))
    try:
        auth.login("https://relay.example.test", timeout=1)
    except auth.LoginError as exc:
        assert "Timed out" in str(exc)
    else:
        raise AssertionError("expected a clear timeout error")
