import io
import json
from unittest.mock import patch

from datamailer_cli.cli import main


def test_inbound_cli_uses_admin_api_and_prints_result(monkeypatch, capsys):
    monkeypatch.setenv('DATAMAILER_URL', 'https://relay.example.net')
    monkeypatch.setenv('DATAMAILER_API_KEY', 'admin-test')
    with patch('datamailer_cli.api.DatamailerClient.request', return_value=(200, {'results': []})) as request:
        assert main(['inbound', 'domains']) == 0
    request.assert_called_once_with('GET', '/api/admin/inbound-domains', payload=None)
    assert json.loads(capsys.readouterr().out) == {'results': []}
    data = {'sender_allowlist': ['alexey@example.net'], 'webhook_secret_id': 'env:BOT_SECRET'}
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(data)))
    with patch('datamailer_cli.api.DatamailerClient.request', return_value=(200, {'id': 12})) as request:
        assert main(['inbound', 'mailboxes', '--id', '12', '--data-file', '-']) == 0
    request.assert_called_once_with('PATCH', '/api/admin/inbound-mailboxes/12', payload=data)
