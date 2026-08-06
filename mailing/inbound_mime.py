import html
import re
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses

SELECTED_HEADERS = (
    "list-unsubscribe",
    "list-unsubscribe-post",
    "x-ses-receipt",
    "x-ses-spam-verdict",
    "x-ses-virus-verdict",
    "x-ses-spf-verdict",
    "x-ses-dkim-verdict",
    "x-ses-dmarc-verdict",
)
LINK_RE = re.compile(r"https?://[^\s<>'\")]+", re.IGNORECASE)
HREF_RE = re.compile(r'''href=["']([^"']+)["']''', re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class PartSummary:
    count: int
    characters: int
    preview: str


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    content_id: str
    disposition: str
    payload: bytes


@dataclass(frozen=True)
class EmailSummary:
    subject: str
    from_: str
    to: str
    cc: str
    date: str
    message_id: str
    sender_addresses: list[str]
    recipient_addresses: list[str]
    selected_headers: dict[str, str]
    text: PartSummary
    html: PartSummary
    links: list[str]
    body_text: str
    body_html: str
    attachments: list[Attachment]


def normalize_preview(value, limit=240):
    value = " ".join((value or "").split())
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."


def address_values(*values):
    headers = [str(value) for value in values if value and str(value).strip()]
    return [addr.lower() for _name, addr in getaddresses(headers) if addr]


def html_to_text(value):
    return html.unescape(TAG_RE.sub(" ", value or ""))


def message_parts(message):
    return list(message.walk()) if message.is_multipart() else [message]


def part_text(part):
    try:
        content = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True)
        if payload is None:
            return str(part.get_payload() or "")
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    return content if isinstance(content, str) else str(content)


def extract_links(text, html_text, selected_headers):
    links = []
    for value in (text or "", html_text or "", "\n".join(selected_headers.values())):
        links.extend(match.rstrip(".,;") for match in LINK_RE.findall(value))
    links.extend(html.unescape(match).rstrip(".,;") for match in HREF_RE.findall(html_text or ""))
    return list(dict.fromkeys(links))


def summarize_parts(parts):
    joined = "\n".join(parts)
    return PartSummary(count=len(parts), characters=len(joined), preview=normalize_preview(joined))


def parse_mime(raw):
    if not raw.strip():
        raise ValueError("empty MIME input")

    message = BytesParser(policy=policy.default).parsebytes(raw)
    if not message.keys():
        raise ValueError("MIME input has no headers")

    text_parts = []
    html_parts = []
    attachments = []
    for part in message_parts(message):
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition() or ""
        filename = part.get_filename() or ""
        if disposition == "attachment" or filename:
            attachments.append(
                Attachment(
                    filename=filename,
                    content_type=part.get_content_type(),
                    content_id=str(part.get("content-id") or "").strip("<>"),
                    disposition=disposition or "inline",
                    payload=part.get_payload(decode=True) or b"",
                )
            )
            continue

        content_type = part.get_content_type()
        if content_type == "text/plain":
            text_parts.append(part_text(part))
        elif content_type == "text/html":
            html_parts.append(part_text(part))

    body_text = "\n".join(text_parts)
    body_html = "\n".join(html_parts)
    selected_headers = {name: str(message[name]) for name in SELECTED_HEADERS if message[name] is not None}
    to = str(message["to"] or "")
    cc = str(message["cc"] or "")
    from_ = str(message["from"] or "")
    return EmailSummary(
        subject=str(message["subject"] or ""),
        from_=from_,
        to=to,
        cc=cc,
        date=str(message["date"] or ""),
        message_id=str(message["message-id"] or "").strip(),
        sender_addresses=address_values(from_),
        recipient_addresses=list(dict.fromkeys(address_values(to, cc, str(message["bcc"] or "")))),
        selected_headers=selected_headers,
        text=summarize_parts(text_parts),
        html=summarize_parts(html_parts),
        links=extract_links(body_text, body_html, selected_headers),
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
    )
