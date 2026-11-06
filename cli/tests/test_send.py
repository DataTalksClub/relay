import urllib.request
from argparse import Namespace

import pytest
from datamailer_cli import api, cli
from datamailer_cli.templates import TEXT_TEMPLATE_KEY

from conftest import FakeServer


@pytest.fixture
def server(monkeypatch):
    fake = FakeServer()
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    return fake


def _send_args(**overrides):
    args = Namespace(
        url="https://x.example.com",
        api_key="dm_test",
        to="me@example.com",
        subject="Results",
        body="all done",
        body_file=None,
        html=False,
        from_=None,
        attachments=[],
        idempotency_key=None,
        json=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_send_provisions_template_on_first_use(server, capsys):
    state = {"provisioned": False}

    def send_handler(payload):
        if not state["provisioned"]:
            return 404, {"error": {"code": "validation_error", "fields": {"template_key": "not_found"}}}
        return 202, {"message": {"id": 7, "email": payload["email"], "status": "queued"}, "idempotent_replay": False}

    def template_handler(payload):
        state["provisioned"] = True
        return 200, {"template": {"key": TEXT_TEMPLATE_KEY}}

    server.route("POST", "/api/transactional/send", send_handler)
    server.route("PUT", f"/api/transactional/templates/{TEXT_TEMPLATE_KEY}", template_handler)

    assert cli.cmd_send(_send_args()) == 0

    methods = [(method, path) for method, path, _ in server.calls]
    assert methods == [
        ("POST", "/api/transactional/send"),
        ("PUT", f"/api/transactional/templates/{TEXT_TEMPLATE_KEY}"),
        ("POST", "/api/transactional/send"),
    ]
    out = capsys.readouterr().out
    assert "message id: 7" in out
    assert "status:     queued" in out


def test_send_passes_subject_and_body_as_context(server):
    server.route(
        "POST",
        "/api/transactional/send",
        lambda payload: (202, {"message": {"id": 1, "email": payload["email"], "status": "queued"}}),
    )
    cli.cmd_send(_send_args(subject="Hi", body="line1\nline2"))
    _, _, payload = server.calls[0]
    assert payload["context"] == {"subject": "Hi", "body": "line1\nline2"}
    assert payload["template_key"] == TEXT_TEMPLATE_KEY


def test_send_requires_recipient(server):
    with pytest.raises(api.ApiError):
        cli.cmd_send(_send_args(to=None))


def test_send_surfaces_suppression_error(server):
    server.route(
        "POST",
        "/api/transactional/send",
        lambda payload: (409, {"error": {"code": "transactional_suppressed", "message": "blocked"}}),
    )
    with pytest.raises(api.ApiError) as excinfo:
        cli.cmd_send(_send_args())
    assert excinfo.value.code == "transactional_suppressed"


def test_send_attaches_csv_file(server, tmp_path):
    csv_file = tmp_path / "list.csv"
    csv_file.write_text("name,email\nAlex,a@x.com\n")
    server.route(
        "POST",
        "/api/transactional/send",
        lambda payload: (202, {"message": {"id": 3, "email": payload["email"], "status": "queued"}}),
    )
    cli.cmd_send(_send_args(attachments=[str(csv_file)]))
    _, _, payload = server.calls[0]
    assert payload["message_parts"] == [
        {
            "content_type": "text/csv",
            "content": "name,email\nAlex,a@x.com\n",
            "filename": "list.csv",
            "disposition": "attachment",
        }
    ]


def test_send_rejects_missing_attachment(server):
    with pytest.raises(api.ApiError):
        cli.cmd_send(_send_args(attachments=["/no/such/file.csv"]))


def test_send_rejects_non_text_attachment(server, tmp_path):
    blob = tmp_path / "photo.png"
    blob.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(100))
    with pytest.raises(api.ApiError):
        cli.cmd_send(_send_args(attachments=[str(blob)]))


def test_send_rejects_oversized_attachment(server, tmp_path):
    big = tmp_path / "big.csv"
    big.write_text("x" * (cli.MAX_ATTACHMENT_BYTES + 1))
    with pytest.raises(api.ApiError):
        cli.cmd_send(_send_args(attachments=[str(big)]))


def _ok_payload(payload, next_id):
    return 202, {"message": {"id": next_id[0], "email": payload["email"], "status": "queued"}}


def test_send_multiple_recipients(server):
    ids = iter([11, 12])
    server.route(
        "POST",
        "/api/transactional/send",
        lambda payload: _ok_payload(payload, [next(ids)]),
    )
    assert cli.cmd_send(_send_args(to=["a@example.com", "b@example.com"])) == 0
    assert [call[2]["email"] for call in server.calls] == [
        "a@example.com",
        "b@example.com",
    ]


def test_send_splits_comma_separated_recipients(server):
    server.route(
        "POST",
        "/api/transactional/send",
        lambda payload: (202, {"message": {"id": 1, "email": payload["email"], "status": "queued"}}),
    )
    cli.cmd_send(_send_args(to=["a@example.com,b@example.com b@example.com"]))
    assert [call[2]["email"] for call in server.calls] == [
        "a@example.com",
        "b@example.com",
    ]


def test_send_scopes_idempotency_key_per_recipient(server):
    server.route(
        "POST",
        "/api/transactional/send",
        lambda payload: (202, {"message": {"id": 1, "email": payload["email"], "status": "queued"}}),
    )
    cli.cmd_send(
        _send_args(
            to=["a@example.com", "b@example.com"],
            idempotency_key="batch-1",
        )
    )
    assert [call[2]["idempotency_key"] for call in server.calls] == [
        "batch-1:a@example.com",
        "batch-1:b@example.com",
    ]


def test_send_keeps_idempotency_key_for_single_recipient(server):
    server.route(
        "POST",
        "/api/transactional/send",
        lambda payload: (202, {"message": {"id": 1, "email": payload["email"], "status": "queued"}}),
    )
    cli.cmd_send(_send_args(to=["a@example.com"], idempotency_key="solo-1"))
    assert server.calls[0][2]["idempotency_key"] == "solo-1"


def test_send_attaches_same_file_to_every_recipient(server, tmp_path):
    csv_file = tmp_path / "list.csv"
    csv_file.write_text("name,email\nAlex,a@x.com\n")
    server.route(
        "POST",
        "/api/transactional/send",
        lambda payload: (202, {"message": {"id": 1, "email": payload["email"], "status": "queued"}}),
    )
    cli.cmd_send(
        _send_args(to=["a@example.com", "b@example.com"], attachments=[str(csv_file)])
    )
    assert len(server.calls) == 2
    for _, _, payload in server.calls:
        assert payload["message_parts"] == [
            {
                "content_type": "text/csv",
                "content": "name,email\nAlex,a@x.com\n",
                "filename": "list.csv",
                "disposition": "attachment",
            }
        ]


def test_split_recipients():
    assert cli.split_recipients("a@example.com") == ["a@example.com"]
    assert cli.split_recipients(["a@example.com", "b@example.com"]) == [
        "a@example.com",
        "b@example.com",
    ]
    assert cli.split_recipients(["a@example.com,b@example.com a@example.com"]) == [
        "a@example.com",
        "b@example.com",
    ]
    assert cli.split_recipients(None) == []
    assert cli.split_recipients([]) == []
