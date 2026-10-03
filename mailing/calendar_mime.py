"""Literal final-content calendar body alternatives; no calendar generation.

The 200,000-character transport limit matches the existing structured-part
policy in services/transactional.py. It counts Python characters, not UTF-8
bytes; the pure MIME owner must not import the transactional service.
"""

import re

MAX_CALENDAR_CONTENT_LENGTH = 200_000
PROTECTED_HEADERS = {"content-type", "mime-version", "content-disposition", "content-transfer-encoding"}
HEADER_NAME = re.compile(r"^[!-9;-~]+$")
COMPONENT_NAME = re.compile(r"[A-Za-z0-9-]+")


def validate_calendar_alternative(value, text_body, html_body, message_parts, headers):
    if value is None:
        return None
    if type(value) is not dict or not {"content", "method"} <= value.keys() <= {"content", "method", "name"}:
        raise ValueError("calendar_alternative must have content, method and optional name")
    if message_parts:
        raise ValueError("calendar_alternative conflicts with message_parts")
    _utf8(text_body, "text_body")
    _utf8(html_body, "html_body")
    content = _utf8(value["content"], "calendar content")
    if len(content) > MAX_CALENDAR_CONTENT_LENGTH:
        raise ValueError("calendar content exceeds the character limit")
    method = value["method"]
    if type(method) is not str or method not in {"REQUEST", "CANCEL"}:
        raise ValueError("calendar method must be REQUEST or CANCEL")
    name = value.get("name", "event.ics")
    _name(name)
    _headers(headers)
    _method(content, method)
    return {"content": content, "method": method, "name": name}


def _utf8(value, label):
    if type(value) is not str:
        raise ValueError(f"{label} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{label} must be valid UTF-8") from None
    return value


def _name(value):
    _utf8(value, "calendar name")
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("calendar name must be a nonempty basename")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("calendar name contains a control character")


def _headers(headers):
    if headers is None:
        return
    if type(headers) is not dict:
        raise ValueError("calendar headers must be a mapping")
    for name, value in headers.items():
        if type(name) is not str or not HEADER_NAME.fullmatch(name):
            raise ValueError("calendar header name is invalid")
        if name.lower() in PROTECTED_HEADERS:
            raise ValueError("calendar MIME headers cannot be overridden")
        if type(value) is not str or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("calendar header value is invalid")


def _unfolded(content):
    if any(ord(char) < 32 and char not in "\r\n\t" for char in content):
        raise ValueError("calendar content contains a control character")
    physical = content.replace("\r\n", "\n")
    if "\r" in physical:
        raise ValueError("calendar line endings must be LF or CRLF")
    physical = physical.split("\n")
    if physical and physical[-1] == "":
        physical.pop()
    lines = []
    for line in physical:
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _method(content, method):
    lines = _unfolded(content)
    if not lines or lines[0].upper() != "BEGIN:VCALENDAR" or lines[-1].upper() != "END:VCALENDAR":
        raise ValueError("calendar content must contain one VCALENDAR envelope")
    stack = []
    methods = []
    calendar_count = 0
    for line in lines:
        key, separator, value = line.partition(":")
        key = key.split(";", 1)[0].upper()
        if key in {"BEGIN", "END"} and (not separator or not COMPONENT_NAME.fullmatch(value)):
            raise ValueError("calendar component marker requires a valid nonempty name")
        if key == "BEGIN":
            if value.upper() == "VCALENDAR":
                calendar_count += 1
                if stack or calendar_count != 1:
                    raise ValueError("calendar content has multiple VCALENDAR envelopes")
            stack.append(value.upper())
        elif key == "END":
            if not stack or stack.pop() != value.upper():
                raise ValueError("calendar component envelope is unbalanced")
        elif key == "METHOD":
            if not separator or stack != ["VCALENDAR"]:
                raise ValueError("calendar METHOD must belong to VCALENDAR")
            methods.append(value)
    if stack or methods != [method]:
        raise ValueError("calendar METHOD must be unique and match the declared method")


def add_calendar_alternative(message, text_body, html_body, calendar):
    """Encode exact UTF-8 bytes without newline normalization or dispositions."""
    message.set_content(
        text_body.encode("utf-8"), maintype="text", subtype="plain", cte="base64", params={"charset": "utf-8"}
    )
    message.add_alternative(
        html_body.encode("utf-8"), maintype="text", subtype="html", cte="base64", params={"charset": "utf-8"}
    )
    message.add_alternative(
        calendar["content"].encode("utf-8"),
        maintype="text",
        subtype="calendar",
        cte="base64",
        params={"charset": "utf-8", "method": calendar["method"], "name": calendar["name"]},
    )
