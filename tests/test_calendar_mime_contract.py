"""Synthetic native transport contracts; never call a real provider."""

from email import policy
from email.parser import BytesParser

import pytest
from django.test import override_settings

from mailing.ses import send_email, send_raw_email


class RecordingSes:
    def __init__(self):
        self.calls = []

    def send_email(self, **params):
        self.calls.append(("simple", params))
        return {"MessageId": "synthetic-simple"}

    def send_raw_email(self, **params):
        self.calls.append(("raw", params))
        return {"MessageId": "synthetic-raw"}


def options(client, **changes):
    values = {
        "ses_client": client,
        "source": "sender@example.invalid",
        "to_email": "recipient@example.invalid",
        "subject": "Synthetic café 🧭",
        "html_body": "<p>Café 🧭</p>\n",
        "text_body": "Café 🧭\n",
        "reply_to": "reply@example.invalid",
        "cc": ["copy@example.invalid"],
        "bcc": ["private@example.invalid"],
    }
    values.update(changes)
    return values


def captured_message(client):
    assert len(client.calls) == 1 and client.calls[0][0] == "raw"
    params = client.calls[0][1]
    message = BytesParser(policy=policy.default).parsebytes(params["RawMessage"]["Data"])
    return params, message


def decoded(part):
    return part.get_payload(decode=True).decode("utf-8")


def assert_envelope(params, message):
    assert params["Source"] == "sender@example.invalid"
    assert params["Destinations"] == ["recipient@example.invalid", "copy@example.invalid", "private@example.invalid"]
    assert message["From"] == "sender@example.invalid"
    assert message["To"] == "recipient@example.invalid"
    assert message["Cc"] == "copy@example.invalid"
    assert message["Reply-To"] == "reply@example.invalid"
    assert message["Subject"] == "Synthetic café 🧭"
    assert message["Bcc"] is None


@pytest.mark.parametrize("entrypoint", [send_email, send_raw_email])
@pytest.mark.parametrize("attachment", [False, True], ids=["header-only", "ordinary-attachment"])
@override_settings(AWS_SES_CONFIGURATION_SET="default-configuration", SES_MAX_SEND_RATE_PER_SECOND=0)
def test_existing_raw_paths_preserve_bodies_envelope_and_attachment(entrypoint, attachment):
    client = RecordingSes()
    parts = []
    if attachment:
        parts = [
            {
                "content_type": "text/calendar; method=REQUEST",
                "content": "Legacy calendar\n",
                "filename": "legacy.ics",
                "disposition": "attachment",
            }
        ]
    result = entrypoint(**options(client, headers={"X-Synthetic": "retained"}, message_parts=parts))
    params, message = captured_message(client)
    assert result == "synthetic-raw" and params["ConfigurationSetName"] == "default-configuration"
    assert_envelope(params, message)
    assert message["X-Synthetic"] == "retained"
    body = message
    if attachment:
        assert message.get_content_type() == "multipart/mixed"
        body, attached = message.get_payload()
        assert attached.get_content_disposition() == "attachment"
        assert attached.get_filename() == "legacy.ics" and decoded(attached) == "Legacy calendar\n"
    assert body.get_content_type() == "multipart/alternative"
    plain, html = body.get_payload()
    assert decoded(plain) == "Café 🧭\n" and decoded(html) == "<p>Café 🧭</p>\n"


@pytest.mark.parametrize("configuration", [None, "", "explicit-configuration"])
@override_settings(AWS_SES_CONFIGURATION_SET="default-configuration", SES_MAX_SEND_RATE_PER_SECOND=0)
def test_existing_simple_payload_retains_options_and_configuration_override(configuration):
    client = RecordingSes()
    result = send_email(**options(client, text_body="", configuration_set=configuration))
    assert result == "synthetic-simple" and len(client.calls) == 1
    kind, params = client.calls[0]
    assert kind == "simple"
    expected = {
        "Source": "sender@example.invalid",
        "Destination": {
            "ToAddresses": ["recipient@example.invalid"],
            "CcAddresses": ["copy@example.invalid"],
            "BccAddresses": ["private@example.invalid"],
        },
        "Message": {
            "Subject": {"Charset": "UTF-8", "Data": "Synthetic café 🧭"},
            "Body": {"Html": {"Charset": "UTF-8", "Data": "<p>Café 🧭</p>\n"}},
        },
        "ReplyToAddresses": ["reply@example.invalid"],
    }
    if configuration is None:
        expected["ConfigurationSetName"] = "default-configuration"
    elif configuration:
        expected["ConfigurationSetName"] = configuration
    assert params == expected


def calendar_text(method):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Synthetic contract//EN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        "UID:stable-synthetic-uid",
        "SEQUENCE:7",
        "DTSTART;TZID=Europe/Berlin:20261025T023000",
        "DTEND;TZID=Europe/Berlin:20261025T033000",
        "ATTENDEE:mailto:recipient@example.invalid",
        "ORGANIZER:mailto:sender@example.invalid",
        "SUMMARY:Café 🧭",
        "DESCRIPTION:{{ literal }} \\nsecond line",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def assert_calendar(message, plain, html, content, method, name):
    assert message.get_content_type() == "multipart/alternative"
    parts = message.get_payload()
    assert [part.get_content_type() for part in parts] == ["text/plain", "text/html", "text/calendar"]
    assert [part.get_content_charset() for part in parts] == ["utf-8", "utf-8", "utf-8"]
    assert [decoded(part) for part in parts] == [plain, html, content]
    assert [part.get("Content-Disposition") for part in message.walk()] == [None, None, None, None]
    assert parts[2].get_param("method") == method
    assert parts[2].get_param("name") == name


@pytest.mark.parametrize("entrypoint", [send_email, send_raw_email])
@pytest.mark.parametrize("method", ["REQUEST", "CANCEL"])
@pytest.mark.parametrize("plain,html", [("Café 🧭\r\n", "<p>Café 🧭</p>"), ("", "<p>Only HTML</p>"), ("Only text", "")])
@override_settings(AWS_SES_CONFIGURATION_SET="default-configuration", SES_MAX_SEND_RATE_PER_SECOND=0)
def test_calendar_is_last_literal_utf8_body_alternative(entrypoint, method, plain, html):
    client = RecordingSes()
    content = calendar_text(method)
    calendar = {"content": content, "method": method}
    name = "event.ics"
    if method == "REQUEST":
        name = "invitation 🧭.ics"
        calendar["name"] = name
    original = dict(calendar)
    result = entrypoint(**options(client, text_body=plain, html_body=html, calendar_alternative=calendar))
    params, message = captured_message(client)
    assert result == "synthetic-raw" and params["ConfigurationSetName"] == "default-configuration"
    assert_envelope(params, message)
    assert_calendar(message, plain, html, content, method, name)
    assert calendar == original


VALID_CALENDAR = {"content": calendar_text("REQUEST"), "method": "REQUEST"}
INVALID_CALENDARS = [
    ("", {}),
    ([], {}),
    (False, {}),
    ({}, {}),
    ({**VALID_CALENDAR, "content": b"bytes"}, {}),
    ({**VALID_CALENDAR, "content": None}, {}),
    ({**VALID_CALENDAR, "content": "\ud800"}, {}),
    ({**VALID_CALENDAR, "method": "REPLY"}, {}),
    ({**VALID_CALENDAR, "method": ["REQUEST"]}, {}),
    ({**VALID_CALENDAR, "method": "CANCEL"}, {}),
    ({**VALID_CALENDAR, "name": "../unsafe.ics"}, {}),
    ({**VALID_CALENDAR, "name": "folder\\unsafe.ics"}, {}),
    ({**VALID_CALENDAR, "name": "unsafe\r\nInjected: canary"}, {}),
    ({**VALID_CALENDAR, "name": ""}, {}),
    ({**VALID_CALENDAR, "name": None}, {}),
    ({**VALID_CALENDAR, "disposition": "attachment"}, {}),
    (
        {
            **VALID_CALENDAR,
            "content": calendar_text("REQUEST").replace("METHOD:REQUEST", "METHOD:REQUEST\r\nMETHOD:REQUEST"),
        },
        {},
    ),
    ({**VALID_CALENDAR, "content": calendar_text("REQUEST") + "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"}, {}),
    (VALID_CALENDAR, {"message_parts": [{"content_type": "text/plain", "content": "conflict"}]}),
    (VALID_CALENDAR, {"text_body": b"not text"}),
    (VALID_CALENDAR, {"html_body": None}),
    (VALID_CALENDAR, {"headers": {"Content-Type": "text/plain"}}),
    (VALID_CALENDAR, {"headers": {"mImE-vErSiOn": "2.0"}}),
    (VALID_CALENDAR, {"headers": {"Content-Disposition": "attachment"}}),
    (VALID_CALENDAR, {"headers": {"Content-Transfer-Encoding": "binary"}}),
    (VALID_CALENDAR, {"headers": {"X-Test\r\nInjected": "canary"}}),
    (VALID_CALENDAR, {"headers": {"X-Test": "canary\r\nInjected: canary"}}),
]


@pytest.mark.parametrize("entrypoint", [send_email, send_raw_email])
@pytest.mark.parametrize("calendar,changes", INVALID_CALENDARS)
@override_settings(AWS_SES_CONFIGURATION_SET="", SES_MAX_SEND_RATE_PER_SECOND=0)
def test_invalid_calendar_contract_never_reaches_provider(entrypoint, calendar, changes):
    client = RecordingSes()
    with pytest.raises(ValueError) as error:
        entrypoint(**options(client, calendar_alternative=calendar, **changes))
    assert client.calls == []
    assert "canary" not in str(error.value)


@override_settings(AWS_SES_CONFIGURATION_SET="", SES_MAX_SEND_RATE_PER_SECOND=0)
def test_full_multibyte_character_limit_preserves_content_and_rejects_one_more():
    content = calendar_text("REQUEST")
    padded = content.replace("SUMMARY:Café 🧭", "SUMMARY:" + "🧭" * (200_000 - len(content) + len("Café 🧭")))
    assert len(padded) == 200_000 and len(padded.encode("utf-8")) > 790_000
    client = RecordingSes()
    send_email(**options(client, calendar_alternative={"content": padded, "method": "REQUEST"}))
    _, message = captured_message(client)
    assert_calendar(message, "Café 🧭\n", "<p>Café 🧭</p>\n", padded, "REQUEST", "event.ics")
    oversized = padded.replace("SUMMARY:", "SUMMARY:🧭", 1)
    assert len(oversized) == 200_001
    blocked = RecordingSes()
    with pytest.raises(ValueError):
        send_email(**options(blocked, calendar_alternative={"content": oversized, "method": "REQUEST"}))
    assert blocked.calls == []


@pytest.mark.parametrize("line_end", ["\n", "\r\n"])
@pytest.mark.parametrize("terminal", [False, True])
@override_settings(AWS_SES_CONFIGURATION_SET="", SES_MAX_SEND_RATE_PER_SECOND=0)
def test_folded_method_validation_keeps_calendar_bytes_and_no_calendar_attachment(line_end, terminal):
    content = calendar_text("REQUEST").replace("METHOD:REQUEST", "METHOD:REQ\r\n UEST")
    content = content.replace("\r\n", line_end)
    if not terminal:
        content = content[: -len(line_end)]
    client = RecordingSes()
    send_email(
        **options(
            client, calendar_alternative={"content": content, "method": "REQUEST"}, headers={"X-Test": "retained"}
        )
    )
    _, message = captured_message(client)
    assert message["X-Test"] == "retained"
    assert_calendar(message, "Café 🧭\n", "<p>Café 🧭</p>\n", content, "REQUEST", "event.ics")


@pytest.mark.parametrize("entrypoint", [send_email, send_raw_email])
@pytest.mark.parametrize(
    "begin,end",
    [
        ("BEGIN", "END"),
        ("BEGIN:", "END:"),
        ("BEGIN:X-TEST", "END"),
        ("BEGIN:X-TEST", "END:"),
        ("BEGIN:BAD NAME", "END:BAD NAME"),
        ("BEGIN:BAD!NAME", "END:BAD!NAME"),
    ],
)
@override_settings(AWS_SES_CONFIGURATION_SET="", SES_MAX_SEND_RATE_PER_SECOND=0)
def test_malformed_component_records_are_redacted_before_provider(entrypoint, begin, end):
    content = calendar_text("REQUEST").replace("BEGIN:VEVENT", f"{begin}\r\nX-SECRET:canary\r\n{end}\r\nBEGIN:VEVENT")
    client = RecordingSes()
    with pytest.raises(ValueError) as error:
        entrypoint(**options(client, calendar_alternative={"content": content, "method": "REQUEST"}))
    assert client.calls == []
    assert "canary" not in str(error.value) and content not in str(error.value)


@pytest.mark.parametrize("entrypoint", [send_email, send_raw_email])
@override_settings(AWS_SES_CONFIGURATION_SET="", SES_MAX_SEND_RATE_PER_SECOND=0)
def test_custom_nested_components_and_series_properties_remain_literal(entrypoint):
    content = calendar_text("REQUEST").replace(
        "SEQUENCE:7", "SEQUENCE:7\r\nRECURRENCE-ID:20261025T023000\r\nX-OPAQUE:{{ literal }}"
    )
    content = content.replace("END:VEVENT", "BEGIN:X-CUSTOM-7\r\nX-DATA:café 🧭\r\nEND:X-CUSTOM-7\r\nEND:VEVENT")
    client = RecordingSes()
    entrypoint(**options(client, calendar_alternative={"content": content, "method": "REQUEST"}))
    _, message = captured_message(client)
    assert_calendar(message, "Café 🧭\n", "<p>Café 🧭</p>\n", content, "REQUEST", "event.ics")
