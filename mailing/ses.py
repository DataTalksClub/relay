import threading
import time
from dataclasses import dataclass
from email.message import EmailMessage, Message

from django.conf import settings

from mailing.calendar_mime import add_calendar_alternative, validate_calendar_alternative
from mailing.dry_run import ensure_transport_allowed

_rate_limit_lock = threading.Lock()
_last_ses_send_monotonic = None


@dataclass(frozen=True)
class _Envelope:
    source: str
    to_email: str
    reply_to: str
    cc: list | None
    bcc: list | None
    configuration_set: str | None


@dataclass(frozen=True)
class _Content:
    subject: str
    html: str
    text: str
    headers: dict | None
    parts: list | None
    calendar: dict | None


def send_email(
    *,
    ses_client,
    source,
    to_email,
    subject,
    html_body,
    text_body="",
    reply_to="",
    cc=None,
    bcc=None,
    headers=None,
    message_parts=None,
    configuration_set=None,
    calendar_alternative=None,
):
    calendar = validate_calendar_alternative(calendar_alternative, text_body, html_body, message_parts, headers)
    envelope = _Envelope(source, to_email, reply_to, cc, bcc, configuration_set)
    content = _Content(subject, html_body, text_body, headers, message_parts, calendar)
    raw = bool(headers or message_parts or calendar is not None)
    parameters = _raw_parameters(envelope, content) if raw else _simple_parameters(envelope, content)
    ensure_transport_allowed("SES send_email")
    throttle_ses_send()
    if raw:
        return ses_client.send_raw_email(**parameters)["MessageId"]
    return ses_client.send_email(**parameters)["MessageId"]


def send_raw_email(
    *,
    ses_client,
    source,
    to_email,
    subject,
    html_body,
    text_body="",
    reply_to="",
    cc=None,
    bcc=None,
    headers=None,
    message_parts=None,
    configuration_set=None,
    calendar_alternative=None,
):
    calendar = validate_calendar_alternative(calendar_alternative, text_body, html_body, message_parts, headers)
    envelope = _Envelope(source, to_email, reply_to, cc, bcc, configuration_set)
    content = _Content(subject, html_body, text_body, headers, message_parts, calendar)
    parameters = _raw_parameters(envelope, content)
    ensure_transport_allowed("SES send_raw_email")
    return ses_client.send_raw_email(**parameters)["MessageId"]


def _simple_parameters(envelope, content):
    body = {"Html": {"Charset": "UTF-8", "Data": content.html}}
    if content.text:
        body["Text"] = {"Charset": "UTF-8", "Data": content.text}
    destination = {"ToAddresses": [envelope.to_email]}
    if envelope.cc:
        destination["CcAddresses"] = envelope.cc
    if envelope.bcc:
        destination["BccAddresses"] = envelope.bcc
    params = {
        "Source": envelope.source,
        "Destination": destination,
        "Message": {"Subject": {"Charset": "UTF-8", "Data": content.subject}, "Body": body},
    }
    if envelope.reply_to:
        params["ReplyToAddresses"] = [envelope.reply_to]
    _configuration(params, envelope.configuration_set)
    return params


def _raw_parameters(envelope, content):
    message = _raw_message(envelope, content)
    params = {
        "Source": envelope.source,
        "Destinations": [envelope.to_email, *(envelope.cc or []), *(envelope.bcc or [])],
        "RawMessage": {"Data": message.as_bytes()},
    }
    _configuration(params, envelope.configuration_set)
    return params


def _configuration(params, configuration_set):
    if configuration_set is None:
        configuration_set = settings.AWS_SES_CONFIGURATION_SET
    if configuration_set:
        params["ConfigurationSetName"] = configuration_set


def _raw_message(envelope, content):
    message = EmailMessage()
    message["From"] = envelope.source
    message["To"] = envelope.to_email
    if envelope.cc:
        message["Cc"] = ", ".join(envelope.cc)
    if envelope.reply_to:
        message["Reply-To"] = envelope.reply_to
    message["Subject"] = content.subject
    for name, value in (content.headers or {}).items():
        message[name] = value
    if content.calendar is not None:
        add_calendar_alternative(message, content.text, content.html, content.calendar)
    else:
        _ordinary_body(message, content)
    return message


def _ordinary_body(message, content):
    if content.text:
        message.set_content(content.text)
        if content.html:
            message.add_alternative(content.html, subtype="html")
    else:
        message.set_content(content.html, subtype="html")
    if content.parts:
        if message.get_content_maintype() != "multipart" or message.get_content_subtype() != "mixed":
            message.make_mixed()
        for part in content.parts:
            add_structured_part(message, part)


def add_structured_part(message, part):
    parsed = parse_content_type(part["content_type"])
    message.add_attachment(
        part["content"],
        subtype=parsed["subtype"],
        params=parsed["params"],
        filename=part.get("filename") or None,
        disposition=part.get("disposition") or "attachment",
    )


def parse_content_type(value):
    message = Message()
    message["content-type"] = value
    content_type = message.get_content_type()
    maintype, subtype = content_type.split("/", 1)
    return {"maintype": maintype, "subtype": subtype, "params": dict(message.get_params()[1:])}


def throttle_ses_send():
    max_rate = float(getattr(settings, "SES_MAX_SEND_RATE_PER_SECOND", 0) or 0)
    if max_rate <= 0:
        return

    min_interval = 1.0 / max_rate
    global _last_ses_send_monotonic
    with _rate_limit_lock:
        now = time.monotonic()
        if _last_ses_send_monotonic is not None:
            elapsed = now - _last_ses_send_monotonic
            if elapsed < min_interval:
                wait_seconds = min_interval - elapsed
                time.sleep(wait_seconds)
                now += wait_seconds
        _last_ses_send_monotonic = now
