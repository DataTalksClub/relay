import binascii
import logging
import threading
import time
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import formataddr, parseaddr

from botocore.exceptions import ClientError
from django.conf import settings

from mailing.calendar_mime import add_calendar_alternative, validate_calendar_alternative
from mailing.dry_run import ensure_transport_allowed
from mailing.ses_rate_limiter import (
    BULK,
    THROTTLING_CODES,
    acquire_ses_permit,
    report_ses_throttling,
)

logger = logging.getLogger(__name__)

# Per-thread phase timings of the last send call, read by the campaign sender
# for its batch log: seconds spent building the SES parameters (MIME) and
# waiting on the global rate limiter.
send_phase_timings = threading.local()

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
    original_mime=None,
    forward_footer=None,
    kind=BULK,
):
    calendar = validate_calendar_alternative(calendar_alternative, text_body, html_body, message_parts, headers)
    envelope = _Envelope(source, to_email, reply_to, cc, bcc, configuration_set)
    content = _Content(subject, html_body, text_body, headers, message_parts, calendar)
    raw = bool(headers or message_parts or calendar is not None)
    build_started = time.perf_counter()
    if original_mime is not None:
        forwarded = _forward_message(envelope, original_mime, footer=forward_footer)
        parameters = {"Source": source, "Destinations": [to_email], "RawMessage": {"Data": forwarded.as_bytes()}}
        _configuration(parameters, configuration_set)
        raw = True
    else:
        parameters = _raw_parameters(envelope, content) if raw else _simple_parameters(envelope, content)
    send_phase_timings.build = time.perf_counter() - build_started
    return _send_with_permit(
        ses_client, parameters, raw=raw, kind=kind, operation="SES send_email",
    )


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
    kind=BULK,
):
    calendar = validate_calendar_alternative(calendar_alternative, text_body, html_body, message_parts, headers)
    envelope = _Envelope(source, to_email, reply_to, cc, bcc, configuration_set)
    content = _Content(subject, html_body, text_body, headers, message_parts, calendar)
    build_started = time.perf_counter()
    parameters = _raw_parameters(envelope, content)
    send_phase_timings.build = time.perf_counter() - build_started
    return _send_with_permit(
        ses_client, parameters, raw=True, kind=kind, operation="SES send_raw_email",
    )


def _recipient_cost(parameters, raw):
    if raw:
        return max(1, len(parameters.get("Destinations") or []))
    destination = parameters.get("Destination") or {}
    return max(
        1,
        len(destination.get("ToAddresses") or [])
        + len(destination.get("CcAddresses") or [])
        + len(destination.get("BccAddresses") or []),
    )


def _send_with_permit(ses_client, parameters, *, raw, kind, operation):
    cost = _recipient_cost(parameters, raw)
    ensure_transport_allowed(operation)
    # Every SES call acquires a global permit first, including raw sends; the
    # SDK client is configured with standard retries (mailing/aws.py), and our
    # layer re-acquires a permit before its single throttling retry, so retries
    # never bypass the account budget.
    waited = 0.0
    started = time.perf_counter()
    acquire_ses_permit(cost=cost, kind=kind)
    waited += time.perf_counter() - started
    send_phase_timings.limiter_wait = waited
    call = ses_client.send_raw_email if raw else ses_client.send_email
    try:
        return call(**parameters)["MessageId"]
    except ClientError as exc:
        if not _is_throttling(exc):
            raise
        report_ses_throttling()
        started = time.perf_counter()
        acquire_ses_permit(cost=cost, kind=kind)
        waited += time.perf_counter() - started
        send_phase_timings.limiter_wait = waited
        return call(**parameters)["MessageId"]


def _is_throttling(exc):
    code = exc.response.get("Error", {}).get("Code", "")
    if code in THROTTLING_CODES:
        return True
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    return status == 429


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


# The stdlib quoted-printable encoder is pure Python (~10 ms for an 80 KB
# newsletter, GIL-held, once per recipient). binascii does the same job in C.
_FAST_QP_MIN_HTML = 8192


def _add_html_alternative(message, html):
    if len(html) < _FAST_QP_MIN_HTML:
        message.add_alternative(html, subtype="html")
        return
    message.make_alternative()
    part = type(message)(policy=message.policy)
    part["Content-Type"] = 'text/html; charset="utf-8"'
    part["Content-Transfer-Encoding"] = "quoted-printable"
    part.set_payload(binascii.b2a_qp(html.encode("utf-8")).decode("ascii"))
    message.attach(part)


def _ordinary_body(message, content):
    if content.text:
        message.set_content(content.text)
        if content.html:
            _add_html_alternative(message, content.html)
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


def throttle_ses_send(*, cost=1, kind=BULK, timeout=None):
    """Acquire a global SES permit. Kept as the named send gate (WP2 scope).

    The old process-local 1/rate pacer is gone: every send path funnels
    through the Postgres token bucket, so K processes share one budget.
    """
    acquire_ses_permit(cost=cost, kind=kind, timeout=timeout)


def _forward_message(envelope, raw, footer=None):
    # Keep the original MIME tree intact, including inline parts, charsets,
    # nested multipart, calendar data, and binary attachments.
    message = BytesParser(policy=policy.SMTP).parsebytes(raw)
    original_from = str(message.get("From", ""))
    display, address = parseaddr(original_from)
    originals = {name: str(message[name]) for name in ("Date", "Message-ID", "References", "In-Reply-To", "Subject") if message[name]}
    for name in list(message.keys()):
        lower = name.lower()
        if lower in {"from", "to", "cc", "bcc", "reply-to", "sender", "return-path", "received", "authentication-results", "dkim-signature", "arc-seal", "arc-message-signature", "arc-authentication-results", "message-id", "date", "auto-submitted", "x-loop"} or lower.startswith("x-ses-"):
            del message[name]
    message["From"] = formataddr((f"{display or address} via {envelope.source.rpartition('@')[2]}", envelope.source))
    message["To"] = envelope.to_email
    message["Reply-To"] = original_from
    message["Auto-Submitted"] = "auto-forwarded"
    message["X-Loop"] = "relay-inbound"
    message["X-Relay-Inbound"] = "forward"
    for name, value in originals.items():
        message["X-Original-" + name] = value
    if footer:
        # Edited on a copy: the footer is a convenience, and a body it cannot
        # handle must still be forwarded unchanged rather than failed or retried.
        candidate = BytesParser(policy=policy.SMTP).parsebytes(message.as_bytes())
        try:
            _append_footer(candidate, *footer)
            message = candidate
        except Exception as exc:
            logger.warning("forward footer skipped error_type=%s", type(exc).__name__)
    return message


def _append_footer(message, text, markup):
    """Add the block-link footer to the readable body without touching attachments.

    Appends to the first inline text/plain and text/html parts. A signed or
    encrypted body cannot be edited without breaking it, and a body with no
    readable part has nothing to append to, so both get a separate inline part.
    """
    done = set()
    if message.get_content_type() not in {"multipart/signed", "multipart/encrypted"}:
        for part in message.walk():
            subtype = part.get_content_subtype()
            if (
                part.get_content_maintype() != "text"
                or subtype not in {"plain", "html"}
                or subtype in done
                or part.get_content_disposition() == "attachment"
                or part.get_filename()
            ):
                continue
            try:
                body = part.get_content()
            except (LookupError, ValueError):
                continue
            if subtype == "plain":
                body = body.rstrip("\n") + text
            else:
                index = body.lower().rfind("</body>")
                body = body[:index] + markup + body[index:] if index >= 0 else body + markup
            part.set_content(body, subtype=subtype, charset="utf-8")
            done.add(subtype)
        if done:
            return
    if message.get_content_type() != "multipart/mixed":
        message.make_mixed()
    message.add_attachment(text.lstrip(), disposition="inline")
