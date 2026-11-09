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


UUID = "6f9619ff-8b86-d011-b42d-00c04fc964ff"


def test_schedule_show_uses_uuid_path():
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(200, {"id": UUID})) as request:
        result = cli.main([
            "operator", "schedules", "show", "--uuid", UUID,
            "--url", "https://relay.test", "--api-key", "relay_admin_test",
        ])
    assert result == 0
    request.assert_called_once_with("GET", f"/api/admin/schedules/{UUID}", payload=None, query=None)


def test_job_retry_requires_confirmation(capsys):
    result = cli.main([
        "operator", "jobs", "retry", "--uuid", UUID,
        "--url", "https://relay.test", "--api-key", "relay_admin_test",
    ])
    assert result == 1
    assert "--confirm" in capsys.readouterr().err


def test_job_retry_posts_confirmed_retry():
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(200, {"id": UUID})) as request:
        result = cli.main([
            "operator", "jobs", "retry", "--uuid", UUID, "--confirm",
            "--url", "https://relay.test", "--api-key", "relay_admin_test",
        ])
    assert result == 0
    request.assert_called_once_with("POST", f"/api/admin/jobs/{UUID}/retry", payload={}, query=None)


def test_dead_letter_list_uses_admin_resource():
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(200, {"items": []})) as request:
        result = cli.main(["operator", "dead-letters", "list", "--url", "https://relay.test", "--api-key", "relay_admin_test"])
    assert result == 0
    request.assert_called_once_with("GET", "/api/admin/dead-letters", payload=None, query=None)


def test_dead_letter_retry_rejects_invalid_uuid(capsys):
    result = cli.main([
        "operator", "dead-letters", "retry", "--uuid", "not-a-uuid", "--confirm",
        "--url", "https://relay.test", "--api-key", "relay_admin_test",
    ])
    assert result == 1
    assert "--uuid" in capsys.readouterr().err


def test_email_activity_list_filters_by_client():
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(200, {"items": []})) as request:
        result = cli.main([
            "operator", "events", "list", "--client-id", "3",
            "--url", "https://relay.test", "--api-key", "relay_admin_test",
        ])
    assert result == 0
    request.assert_called_once_with("GET", "/api/admin/events", payload=None, query={"client_id": 3})


def test_stats_workers_reports_service_health():
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(200, {"workers": []})) as request:
        result = cli.main(["operator", "stats", "workers", "--url", "https://relay.test", "--api-key", "relay_admin_test"])
    assert result == 0
    request.assert_called_once_with("GET", "/api/admin/workers", payload=None, query=None)


def test_audience_overview_uses_overview_endpoint():
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(200, {"overview": {}})) as request:
        result = cli.main([
            "operator", "lists", "overview", "--id", "5",
            "--url", "https://relay.test", "--api-key", "relay_admin_test",
        ])
    assert result == 0
    request.assert_called_once_with("GET", "/api/admin/audiences/5/overview", payload=None, query=None)


def test_campaign_recipient_assume_sent_posts_with_confirmation():
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(200, {"id": 8})) as request:
        result = cli.main([
            "operator", "campaigns", "assume-sent", "--id", "4", "--recipient-id", "8", "--confirm",
            "--url", "https://relay.test", "--api-key", "relay_admin_test",
        ])
    assert result == 0
    request.assert_called_once_with("POST", "/api/admin/campaigns/4/recipients/8/assume-sent", payload={}, query=None)


def test_contact_csv_export_prints_raw_csv():
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(200, {"raw": "email\nx@y.z\n"})) as request:
        result = cli.main([
            "operator", "contacts", "csv-export", "--client-id", "3", "--query", "x@y.z",
            "--url", "https://relay.test", "--api-key", "relay_admin_test",
        ])
    assert result == 0
    request.assert_called_once_with(
        "GET", "/api/admin/clients/3/contacts.csv", payload=None, query={"q": "x@y.z"},
    )


def test_contact_csv_import_requires_dry_run_or_confirmation(tmp_path, capsys):
    data_file = tmp_path / "contacts.csv.json"
    data_file.write_text('{"csv": "email\\nx@y.z\\n"}')
    result = cli.main([
        "operator", "contacts", "csv-import", "--client-id", "3", "--data-file", str(data_file),
        "--url", "https://relay.test", "--api-key", "relay_admin_test",
    ])
    assert result == 1
    assert "--confirm" in capsys.readouterr().err

    dry_run_file = tmp_path / "contacts-dry.json"
    dry_run_file.write_text('{"csv": "email\\nx@y.z\\n", "dry_run": true}')
    with patch("datamailer_cli.api.DatamailerClient.request", return_value=(200, {"created": 1})) as request:
        result = cli.main([
            "operator", "contacts", "csv-import", "--client-id", "3", "--data-file", str(dry_run_file),
            "--url", "https://relay.test", "--api-key", "relay_admin_test",
        ])
    assert result == 0
    request.assert_called_once_with(
        "POST", "/api/admin/clients/3/contacts/imports/csv",
        payload={"csv": "email\nx@y.z\n", "dry_run": True}, query=None,
    )
