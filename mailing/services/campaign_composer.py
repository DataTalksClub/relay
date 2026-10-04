"""Validated, portable email content from the operator's simple block editor."""

import json
from html import escape
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError

EDITOR_METADATA_KEY = "operator_editor"
BLOCK_TYPES = {"heading", "paragraph", "button", "divider"}


def parse_blocks(raw):
    if len(raw) > 100_000:
        raise ValidationError("The message is too large for the visual composer. Use HTML source instead.")
    try:
        blocks = json.loads(raw or "[]")
    except (ValueError, TypeError):
        raise ValidationError("The composer content could not be read. Review the message and save again.") from None
    if not isinstance(blocks, list) or len(blocks) > 40:
        raise ValidationError("Use at most 40 content blocks.")
    validated = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") not in BLOCK_TYPES:
            raise ValidationError("The message contains an unsupported content block.")
        kind = block["type"]
        text = block.get("text", "")
        if not isinstance(text, str) or len(text) > 10_000:
            raise ValidationError("Each block can contain up to 10,000 characters.")
        cleaned = {"type": kind, "text": text.strip()}
        if kind == "button":
            url = block.get("url", "")
            if not isinstance(url, str) or len(url) > 2_000:
                raise ValidationError("Enter a shorter button URL.")
            url = url.strip()
            try:
                parsed = urlsplit(url)
            except ValueError:
                raise ValidationError("Enter a complete http:// or https:// button URL.") from None
            if url and (parsed.scheme not in {"http", "https"} or not parsed.hostname
                        or parsed.username or parsed.password or any(ord(c) < 33 for c in url)):
                raise ValidationError("Enter a complete http:// or https:// button URL.")
            cleaned["url"] = url
        validated.append(cleaned)
    return validated


def render_blocks(blocks):
    html, plain = [], []
    for block in blocks:
        kind, text = block["type"], block["text"]
        safe = escape(text).replace("\n", "<br>")
        if kind == "heading":
            html.append(f'<h1 style="font-size:24px;line-height:1.3;margin:0 0 20px">{safe}</h1>')
            plain.append(text)
        elif kind == "paragraph":
            html.append(f'<p style="margin:0 0 20px;line-height:1.6">{safe}</p>')
            plain.append(text)
        elif kind == "button":
            url = escape(block["url"], quote=True)
            if url:
                html.append(f'<p style="margin:24px 0"><a href="{url}" style="display:inline-block;'
                            f'background:#1f5c94;color:#ffffff;padding:12px 20px;text-decoration:none;'
                            f'border-radius:4px">{safe}</a></p>')
                plain.append(f'{text}: {block["url"]}')
            else:
                html.append(f'<p>{safe}</p>')
                plain.append(text)
        else:
            html.append('<hr style="border:0;border-top:1px solid #d0d7de;margin:24px 0">')
            plain.append("---")
    if not any(block["text"] for block in blocks if block["type"] != "divider"):
        return "", ""
    document = ('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
                '</head><body style="margin:0;background:#ffffff;color:#1f2328;font-family:Arial,sans-serif">'
                '<table role="presentation" style="width:100%;border-collapse:collapse"><tr><td>'
                '<div style="max-width:600px;margin:0 auto;padding:32px 20px">'
                + "".join(html) + '</div></td></tr></table></body></html>')
    return document, "\n\n".join(plain)


def composer_send_issues(campaign):
    metadata = campaign.metadata if isinstance(campaign.metadata, dict) else {}
    editor = metadata.get(EDITOR_METADATA_KEY)
    if not isinstance(editor, dict):
        return []
    try:
        blocks = parse_blocks(json.dumps(editor.get("blocks", [])))
    except ValidationError:
        return ["Review the visual composer content before sending."]
    if any(block["type"] == "button" and (not block["text"] or not block.get("url")) for block in blocks):
        return ["Add a label and destination URL to every button before sending."]
    return []
