from unittest.mock import patch

from datamailer_cli import cli


def test_campaign_list_uses_admin_api():
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(200, {"items": []})) as request:
        assert cli.main(["operator", "campaigns", "list", "--url", "https://relay.test", "--api-key", "relay_admin_test"]) == 0
    request.assert_called_once_with("GET", "/api/admin/campaigns", payload=None, query=None)


def test_campaign_queue_requires_explicit_confirmation(capsys):
    result = cli.main(["operator", "campaigns", "queue", "--id", "7", "--url", "https://relay.test", "--api-key", "relay_admin_test"])
    assert result == 1
    assert "--confirm" in capsys.readouterr().err


def test_campaign_queue_posts_confirmation():
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(200, {"queued": 1})) as request:
        assert cli.main(["operator", "campaigns", "queue", "--id", "7", "--confirm", "--url", "https://relay.test", "--api-key", "relay_admin_test"]) == 0
    request.assert_called_once_with("POST", "/api/admin/campaigns/7/queue", payload={"confirm": True}, query=None)


def test_archive_export_sends_page_parameters():
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(200, {"rows": []})) as request:
        assert cli.main(["operator", "archive", "export", "--resource-name", "contacts", "--offset", "20", "--limit", "50", "--url", "https://relay.test", "--api-key", "relay_admin_test"]) == 0
    request.assert_called_once_with(
        "GET", "/api/admin/transfer/export", payload=None,
        query={"offset": 20, "limit": 50, "resource": "contacts"},
    )


def test_campaign_recipients_use_explicit_client_and_audience_scope():
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(200, {"recipients": []})) as request:
        result = cli.main([
            "operator", "campaigns", "recipients", "--client-id", "3", "--client-slug", "website",
            "--audience", "weekly", "--key", "issue-42", "--url", "https://relay.test",
            "--api-key", "relay_admin_test",
        ])
    assert result == 0
    request.assert_called_once_with(
        "GET", "/api/admin/clients/3/campaigns/issue-42/recipients", payload=None,
        query={"client": "website", "audience": "weekly"},
    )


def test_newsletter_block_type_list_uses_admin_scoped_api():
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(200, {"block_types": []})) as request:
        result = cli.main([
            "operator", "newsletters", "block-types", "--client-id", "3", "--url", "https://relay.test",
            "--api-key", "relay_admin_test",
        ])
    assert result == 0
    request.assert_called_once_with(
        "GET", "/api/admin/clients/3/newsletter-block-types", payload=None, query=None
    )


def test_recipient_list_members_use_client_scope_and_limit():
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(200, {"members": []})) as request:
        result = cli.main([
            "operator", "lists", "members", "--client-id", "3", "--client-slug", "website",
            "--audience", "weekly", "--key", "daily-sync", "--limit", "75", "--include-removed",
            "--url", "https://relay.test", "--api-key", "relay_admin_test",
        ])
    assert result == 0
    request.assert_called_once_with(
        "GET", "/api/admin/clients/3/recipient-lists/daily-sync/members", payload=None,
        query={"client": "website", "audience": "weekly", "limit": 75, "include_removed": "true"},
    )


def test_contact_state_update_requires_confirmation(capsys):
    result = cli.main([
        "operator", "contacts", "state", "--id", "12",
        "--url", "https://relay.test", "--api-key", "relay_admin_test",
    ])
    assert result == 1
    assert "--confirm" in capsys.readouterr().err


def test_complaints_list_filters_contact_search_to_complained_contacts():
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(200, {"items": []})) as request:
        result = cli.main(["operator", "complaints", "list", "--url", "https://relay.test", "--api-key", "relay_admin_test"])
    assert result == 0
    request.assert_called_once_with(
        "GET", "/api/admin/contacts", payload=None, query={"suppression": "complained"}
    )


def test_regular_template_create_uses_operator_write_api(tmp_path):
    data_file = tmp_path / "template.json"
    data_file.write_text('{"client_id": 3, "key": "welcome", "name": "Welcome", "subject": "Hi"}')
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(201, {"id": 19})) as request:
        result = cli.main([
            "operator", "templates", "create", "--data-file", str(data_file), "--url", "https://relay.test",
            "--api-key", "relay_admin_test",
        ])
    assert result == 0
    request.assert_called_once_with(
        "POST", "/api/admin/templates",
        payload={"client_id": 3, "key": "welcome", "name": "Welcome", "subject": "Hi"}, query=None,
    )


def test_regular_template_delete_requires_confirmation(capsys):
    result = cli.main([
        "operator", "templates", "delete", "--id", "19", "--url", "https://relay.test",
        "--api-key", "relay_admin_test",
    ])
    assert result == 1
    assert "--confirm" in capsys.readouterr().err
