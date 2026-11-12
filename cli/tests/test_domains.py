import io
import json
from unittest.mock import patch

import pytest
from datamailer_cli.cli import main


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("DATAMAILER_URL", "https://relay.example.net")
    monkeypatch.setenv("DATAMAILER_API_KEY", "test")


def test_domains_admin_registration(monkeypatch):
    payload = {"name": "dtcdev.click", "organization_id": 1}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(201, {"id": 1})) as request:
        assert main(["domains", "--admin", "--data-file", "-"]) == 0
    request.assert_called_once_with("POST", "/api/admin/sending-domains", payload=payload)


def test_domains_verify_and_client_list():
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(200, {})) as request:
        assert main(["domains", "--admin", "--id", "2", "--verify"]) == 0
        request.assert_called_with("POST", "/api/admin/sending-domains/2/verify", payload=None)
        assert main(["domains"]) == 0
        request.assert_called_with("GET", "/api/client/sending-domains", payload=None)
        assert main(["domains", "--verify"]) == 1


def test_sender_edits_preserve_others_and_default_only_works():
    current = {
        "default_sender_id": "old",
        "senders": [{"id": "old", "email": "old@example.net"}, {"id": "alerts", "email": "alerts@example.net"}],
    }
    with (
        patch("datamailer_cli.api.DatamailerClient.get_senders", return_value=current),
        patch("datamailer_cli.api.DatamailerClient.set_senders", return_value=current) as write,
    ):
        assert main(["senders", "--default", "alerts"]) == 0
        assert write.call_args.args[0] == {**current, "default_sender_id": "alerts"}
        assert main(["senders", "--set", "alerts=DTC Alerts <alerts@example.net>"]) == 0
        assert write.call_args.args[0]["senders"][0] == current["senders"][0]
        assert main(["senders", "--delete", "old"]) == 1
        assert main(["senders", "--delete", "old", "--default", "alerts"]) == 0
        assert write.call_args.args[0]["senders"] == [current["senders"][1]]
