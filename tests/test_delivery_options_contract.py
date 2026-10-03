"""Existing native delivery option validation, preparation and provider fidelity."""

import copy
import json
import socket
from email import policy
from email.parser import BytesParser

import pytest
from django.apps import apps
from django.urls import reverse

from mailing.models import Client, Contact, EmailTemplate, Organization, TransactionalMessage
from mailing.services import transactional as native
from mailing.services.auth import create_client_api_key
from mailing.services.senders import resolve_sender_email
from mailing.services.transactional_versions import resolve_render_source
from mailing.sqs import records_from_messages
from mailing.workers import transactional_email_handler

pytestmark = pytest.mark.django_db(transaction=True)


def forbidden_network(*args, **kwargs):
    raise AssertionError("Synthetic contract must not use network")


@pytest.fixture(autouse=True)
def offline(monkeypatch, settings):
    monkeypatch.setattr(socket.socket, "connect", forbidden_network)
    settings.SES_MAX_SEND_RATE = 0
    settings.AWS_SES_CONFIGURATION_SET = "synthetic-config"


@pytest.fixture
def boundary(monkeypatch):
    organization = Organization.objects.create(name="Synthetic", slug="synthetic")
    owner = Client.objects.create(
        organization=organization,
        name="Owner",
        slug="owner",
        default_sender_id="selected",
        sender_emails=[{"id": "selected", "email": "selected@example.invalid"}],
    )
    create_client_api_key(client=owner, name="Synthetic", raw_api_key="synthetic-key")
    template = EmailTemplate.objects.create(
        client=owner,
        key="notice",
        name="Notice",
        subject="Café",
        html_body="<p>Café</p>",
        text_body="Café",
    )
    queued = []
    monkeypatch.setattr(native, "enqueue_transactional_email", queued.append)
    return owner, template, queued


def request_payload(**options):
    return {"email": "recipient@example.invalid", "template_key": "notice", "idempotency_key": "synthetic", **options}


def post(client, payload):
    return client.post(
        reverse("mailing:api_transactional_send"),
        data=payload,
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer synthetic-key",
    )


def database_counts():
    counts = {}
    for model in apps.get_models():
        if model._meta.app_label in {"mailing", "jobs", "django_tasks_db"}:
            counts[model._meta.label] = model.objects.count()
    return counts


@pytest.mark.parametrize("options", [{}, {"reply_to": None, "cc": [], "bcc": "", "headers": {}, "message_parts": []}])
def test_builder_retains_metadata_identity_without_truthy_overlay(boundary, options):
    owner, template, _ = boundary
    metadata = {"reply_to": "old@example.invalid", "headers": {"X-Old": "old"}, "custom": [1]}
    payload = request_payload(metadata=metadata, context={}, **options)
    original = copy.deepcopy(payload)
    contact = Contact.objects.create(email=payload["email"])
    message = native.build_transactional_message(
        client=owner,
        contact=contact,
        template=template,
        source=resolve_render_source(template),
        payload=payload,
        sender=resolve_sender_email(owner),
        idempotency_key="synthetic",
        status="queued",
    )
    assert message.metadata is metadata
    assert payload == original and message.subject == "Café"


@pytest.mark.parametrize(
    "value,expected,errors",
    [
        (None, "", {}),
        ("", "", {}),
        (" reply@example.invalid ", "reply@example.invalid", {}),
        ("café@example.invalid", "café@example.invalid", {"reply_to": "invalid"}),
        (False, "", {"reply_to": "must_be_non_empty_string"}),
        ("bad", "bad", {"reply_to": "invalid"}),
    ],
)
def test_singular_address_current_result_and_error(value, expected, errors):
    actual = {}
    assert native.validate_optional_email_address({"reply_to": value}, "reply_to", actual) == expected
    assert actual == errors


@pytest.mark.parametrize(
    "value,expected,errors",
    [
        (None, [], {}),
        ("one@example.invalid", ["one@example.invalid"], {}),
        (
            [" one@example.invalid ", "", "bad", 7, "two@example.invalid"],
            ["one@example.invalid", "two@example.invalid"],
            {"cc.1": "must_be_non_empty_string", "cc.2": "invalid", "cc.3": "must_be_non_empty_string"},
        ),
        (7, [], {"cc": "must_be_list"}),
    ],
)
def test_address_list_accumulates_invalid_entries(value, expected, errors):
    actual = {}
    assert native.validate_optional_email_addresses({"cc": value}, "cc", actual) == expected
    assert actual == errors


def test_headers_preserve_spacing_case_and_ordered_refusals():
    errors = {}
    value = {" X-Keep ": "  café  ", "sUbJeCt": "secret", "Bad Name": "x", "X-Line": "a\r\nb", "X-Type": 7}
    assert native.validate_headers(value, errors) == {"X-Keep": "  café  "}
    assert list(errors.items()) == [
        ("headers.sUbJeCt", "reserved"),
        ("headers.Bad Name", "invalid_name"),
        ("headers.X-Line", "invalid_value"),
        ("headers.X-Type", "invalid_value"),
    ]


@pytest.mark.parametrize("count,expected", [(20, {}), (21, {"headers": "too_many"})])
def test_exact_header_limit(count, expected):
    value = {f"X-{index}": "value" for index in range(count)}
    errors = {}
    result = native.validate_headers(value, errors)
    assert errors == expected
    if expected:
        assert result == {}
    else:
        assert result == value


def test_mixed_parts_preserve_short_circuits_candidates_and_error_order():
    errors = {}
    value = [
        7,
        {"content_type": "", "content": False},
        {"content_type": "image/png", "filename": "/bad"},
        {"content_type": " text/custom; x=one ", "content": 7, "filename": "/bad", "disposition": "wrong"},
        {"content_type": "not-a-mime", "content": "", "filename": None, "disposition": ""},
    ]
    assert native.validate_message_parts(value, errors) == [
        {"content_type": "text/custom; x=one", "content": "", "filename": "/bad", "disposition": "wrong"},
        {"content_type": "not-a-mime", "content": "", "filename": "", "disposition": "attachment"},
    ]
    assert list(errors.items()) == [
        ("message_parts.0", "must_be_object"),
        ("message_parts.1.content_type", "required"),
        ("message_parts.2.content_type", "unsupported"),
        ("message_parts.3.content", "must_be_string"),
        ("message_parts.3.filename", "invalid"),
        ("message_parts.3.disposition", "invalid"),
    ]
    assert native.parse_content_type("text/custom; x=one")["params"] == {"x": "one"}


@pytest.mark.parametrize("count,error", [(200000, {}), (200001, {"message_parts.0.content": "too_large"})])
def test_part_capacity_counts_multibyte_characters(count, error):
    content = "🧭" * count
    errors = {}
    result = native.validate_message_parts([{"content_type": "text/plain", "content": content}], errors)
    assert errors == error and result[0]["content"] == content
    assert len(content.encode()) == count * 4


@pytest.mark.parametrize("count,error", [(10, {}), (11, {"message_parts": "too_many"})])
def test_exact_part_count(count, error):
    errors = {}
    result = native.validate_message_parts([{"content_type": "text/plain", "content": ""}] * count, errors)
    assert errors == error
    if error:
        assert result == []
    else:
        assert len(result) == count


@pytest.mark.parametrize(
    "options,error",
    [
        ({"headers": {"sUbJeCt": "replace"}}, {"headers.sUbJeCt": "reserved"}),
        ({"reply_to": "bad\r\nInjected: x"}, {"reply_to": "invalid"}),
        ({"headers": {"X-Value": "bad\nvalue"}}, {"headers.X-Value": "invalid_value"}),
    ],
)
def test_invalid_native_api_options_have_zero_effects(boundary, client, monkeypatch, options, error):
    _, _, queued = boundary
    monkeypatch.setattr("mailing.services.transactional_sender.ses_client_for_source", forbidden_network)
    before = database_counts()
    response = post(client, request_payload(**options))
    assert response.status_code == 400 and response.json()["error"]["fields"] == error
    assert database_counts() == before and queued == []


def selected_options():
    return {
        "reply_to": "reply@example.invalid",
        "cc": ["copy@example.invalid"],
        "bcc": ["hidden@example.invalid"],
        "headers": {"X-Notice": "café"},
        "message_parts": [
            {
                "content_type": "text/plain; x=one",
                "content": "🧭 attachment",
                "filename": "note.txt",
                "disposition": "attachment",
            }
        ],
    }


def queue_event(queued):
    envelope = {"MessageId": "synthetic", "ReceiptHandle": "synthetic", "Body": json.dumps(queued[0])}
    return records_from_messages([envelope])


class Provider:
    def __init__(self):
        self.calls = []

    def send_raw_email(self, **values):
        self.calls.append(values)
        return {"MessageId": "synthetic-id"}


def assert_provider_projection(provider):
    values = provider.calls[0]
    assert values["Source"] == "selected@example.invalid"
    assert values["Destinations"] == ["recipient@example.invalid", "copy@example.invalid", "hidden@example.invalid"]
    mime = BytesParser(policy=policy.default).parsebytes(values["RawMessage"]["Data"])
    assert mime["Reply-To"] == "reply@example.invalid" and mime["X-Notice"] == "café"
    assert mime["Subject"] == "Café" and mime["Bcc"] is None
    assert mime.get_body(preferencelist=("plain",)).get_content() == "Café\n"
    assert mime.get_body(preferencelist=("html",)).get_content() == "<p>Café</p>\n"
    attachment = next(mime.iter_attachments())
    assert attachment.get_filename() == "note.txt" and attachment.get_content() == "🧭 attachment\n"


def test_real_api_replay_queue_worker_keeps_original_options(boundary, client, monkeypatch):
    owner, _, queued = boundary
    provider = Provider()
    monkeypatch.setattr("mailing.services.transactional_sender.ses_client_for_source", lambda source: provider)
    monkeypatch.setattr(
        "mailing.services.transactional_sender.configuration_set_for_source", lambda source: "synthetic-config"
    )
    metadata = {
        "reply_to": "old@example.invalid",
        "cc": ["old@example.invalid"],
        "bcc": ["old@example.invalid"],
        "headers": {"X-Old": "old"},
        "message_parts": [],
        "provenance": "original",
    }
    payload = request_payload(metadata=metadata, **selected_options())
    assert post(client, payload).status_code == 202
    message = TransactionalMessage.objects.get()
    assert message.metadata == metadata | selected_options()
    replay = post(client, request_payload(reply_to="changed@example.invalid", headers={"X-Changed": "yes"}))
    assert replay.status_code == 202 and replay.json()["idempotent_replay"] is True
    assert len(queued) == 1
    owner.sender_emails = [{"id": "selected", "email": "changed@example.invalid"}]
    owner.save(update_fields=["sender_emails"])
    payload["headers"]["X-Notice"] = "changed"
    message.refresh_from_db()
    assert message.metadata == metadata | selected_options()
    response = transactional_email_handler(queue_event(queued), None)
    assert response == {"batchItemFailures": []} and len(provider.calls) == 1
    assert_provider_projection(provider)
