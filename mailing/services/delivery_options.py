"""Pure native address/header/content-part validation and metadata projection."""

import re
from email.message import Message

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from mailing.calendar_mime import validate_calendar_alternative as validate_calendar_mime_alternative

HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,80}$")
RESERVED_HEADERS = {"bcc", "cc", "content-type", "from", "reply-to", "subject", "to"}
MAX_CUSTOM_HEADERS = 20
MAX_MESSAGE_PARTS = 10
MAX_MESSAGE_PART_CONTENT_LENGTH = 200_000


def validate_optional_email_address(data, field, errors):
    value = data.get(field, "")
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not value.strip():
        errors[field] = "must_be_non_empty_string"
        return ""
    value = value.strip()
    try:
        validate_email(value)
    except ValidationError:
        errors[field] = "invalid"
    return value


def validate_optional_email_addresses(data, field, errors):
    value = data.get(field, [])
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw_values = [value]
    elif isinstance(value, list):
        raw_values = value
    else:
        errors[field] = "must_be_list"
        return []

    addresses = []
    for index, raw in enumerate(raw_values):
        if not isinstance(raw, str) or not raw.strip():
            errors[f"{field}.{index}"] = "must_be_non_empty_string"
            continue
        address = raw.strip()
        try:
            validate_email(address)
        except ValidationError:
            errors[f"{field}.{index}"] = "invalid"
            continue
        addresses.append(address)
    return addresses


def validate_headers(value, errors):
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        errors["headers"] = "must_be_object"
        return {}
    if len(value) > MAX_CUSTOM_HEADERS:
        errors["headers"] = "too_many"
        return {}

    headers = {}
    for raw_name, raw_value in value.items():
        name = ""
        if isinstance(raw_name, str):
            name = raw_name.strip()
        field = f"headers.{name or raw_name}"
        if not HEADER_NAME_RE.match(name):
            errors[field] = "invalid_name"
            continue
        if name.casefold() in RESERVED_HEADERS:
            errors[field] = "reserved"
            continue
        if not isinstance(raw_value, str) or "\r" in raw_value or "\n" in raw_value:
            errors[field] = "invalid_value"
            continue
        headers[name] = raw_value
    return headers


def validate_message_parts(value, errors):
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        errors["message_parts"] = "must_be_list"
        return []
    if len(value) > MAX_MESSAGE_PARTS:
        errors["message_parts"] = "too_many"
        return []
    parts = []
    for index, raw_part in enumerate(value):
        part = _validate_part(raw_part, index, errors)
        if part is not None:
            parts.append(part)
    return parts


def _validate_part(raw_part, index, errors):
    field = f"message_parts.{index}"
    if not isinstance(raw_part, dict):
        errors[field] = "must_be_object"
        return None
    content_type = raw_part.get("content_type")
    if not isinstance(content_type, str) or not content_type.strip():
        errors[f"{field}.content_type"] = "required"
        return None
    parsed = parse_content_type(content_type)
    if parsed is None or parsed["maintype"] != "text":
        errors[f"{field}.content_type"] = "unsupported"
        return None
    content = _part_content(raw_part.get("content"), field, errors)
    filename = _part_filename(raw_part.get("filename", ""), field, errors)
    disposition = _part_disposition(raw_part.get("disposition", "attachment"), field, errors)
    return {"content_type": content_type.strip(), "content": content, "filename": filename, "disposition": disposition}


def _part_content(content, field, errors):
    if not isinstance(content, str):
        errors[f"{field}.content"] = "must_be_string"
        return ""
    if len(content) > MAX_MESSAGE_PART_CONTENT_LENGTH:
        errors[f"{field}.content"] = "too_large"
    return content


def _part_filename(filename, field, errors):
    if filename in (None, ""):
        return ""
    if not isinstance(filename, str) or "/" in filename or "\\" in filename:
        errors[f"{field}.filename"] = "invalid"
    return filename


def _part_disposition(disposition, field, errors):
    if disposition in (None, ""):
        return "attachment"
    if disposition not in {"attachment", "inline"}:
        errors[f"{field}.disposition"] = "invalid"
    return disposition


def parse_content_type(value):
    message = Message()
    message["content-type"] = value
    content_type = message.get_content_type()
    if "/" not in content_type:
        return None
    maintype, subtype = content_type.split("/", 1)
    return {
        "maintype": maintype,
        "subtype": subtype,
        "params": dict(message.get_params()[1:]),
    }


def validate_calendar_alternative_option(value, message_parts, headers, errors, text_body="", html_body=""):
    """Map the MIME-stage calendar ValueErrors onto the API error codes.

    Request validation calls this before rendering with the default empty
    bodies, which are always valid UTF-8, so those checks inside the MIME
    validator pass vacuously there; the parsed message_parts and headers
    carry the conflict and protected-header rules so the cross-field coupling
    is enforced before any durable effect. Post-render revalidation passes
    the actually rendered bodies so the same validator and error mapping
    decide on the final artifact.
    """
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        errors["calendar_alternative"] = "must_be_object"
        return None
    try:
        return validate_calendar_mime_alternative(value, text_body, html_body, message_parts, headers)
    except ValueError as exc:
        if message_parts:
            errors["calendar_alternative"] = "conflicts_with_message_parts"
        elif "exceeds the character limit" in str(exc):
            errors["calendar_alternative"] = "too_large"
        else:
            errors["calendar_alternative"] = "invalid"
        return None


def delivery_option_metadata(payload):
    # calendar_alternative is owned by the validated top-level request field;
    # a generic metadata key never carries a validated profile, so it is
    # stripped before the authoritative projection below re-adds one.
    metadata = payload["metadata"]
    if "calendar_alternative" in metadata:
        metadata = metadata.copy()
        del metadata["calendar_alternative"]
    for field in ("reply_to", "cc", "bcc", "headers", "message_parts", "calendar_alternative"):
        if payload.get(field):
            metadata = metadata | {field: payload[field]}
    return metadata
