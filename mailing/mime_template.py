"""Build the campaign MIME once per batch; substitute per-recipient bytes.

The template is the full RFC 5322 message with sentinel placeholders for the
per-recipient values: the To address, the tracking/unsubscribe tokens in the
bodies, and the List-Unsubscribe header. Text parts are encoded 8bit (raw
UTF-8 passthrough) so no encoder ever wraps a line mid-sentinel: quoted-printable
inserts soft breaks inside long lines (verified: ``RLYTRK0SENTIN=\nEL0TOKEN``),
and base64 would hide the sentinels entirely.

The build proves the sentinels survived by counting every sentinel in the
output bytes against the pre-encoding strings; a mismatch raises loudly and
the sender falls back to the per-message builder for that batch. Rendering is
three ``bytes.replace`` calls plus a zero-remainder check, so per-recipient
MIME cost drops from milliseconds to microseconds.
"""

import logging
from dataclasses import dataclass

from mailing.ses import _Content, _Envelope, _raw_message

logger = logging.getLogger(__name__)

TRACKING_SENTINEL = "RLYTRK0SENTINEL0TOKEN"
UNSUBSCRIBE_SENTINEL = "RLYUNS0SENTINEL0TOKEN"
# A deliverable-looking address in .invalid (RFC 2606): it only ever appears
# in the template's To header and is replaced before any send.
TO_SENTINEL = "rly-to-sentinel-7f3a9c2e@relay.invalid"

_SENTINELS = (TRACKING_SENTINEL, UNSUBSCRIBE_SENTINEL, TO_SENTINEL)
_SENTINEL_BYTES = tuple(sentinel.encode("ascii") for sentinel in _SENTINELS)


class MimeTemplateError(Exception):
    """The template is corrupt or a per-recipient value is unsafe to substitute."""


@dataclass(frozen=True)
class CampaignMimeTemplate:
    """One batch's MIME with sentinels; immutable and safe to share across threads."""

    raw: bytes


def build_campaign_template(*, source, subject, html_body, text_body, headers=None, reply_to=""):
    """Serialize the full message once with sentinels for per-recipient values.

    ``html_body``/``text_body`` must already contain TRACKING_SENTINEL and
    UNSUBSCRIBE_SENTINEL where the tokens go, and ``headers`` the
    UNSUBSCRIBE_SENTINEL inside List-Unsubscribe (see
    ``campaign_sender._template_bodies``). Raises MimeTemplateError if any
    sentinel does not survive encoding intact.
    """
    headers = dict(headers or {})
    envelope = _Envelope(source, TO_SENTINEL, reply_to or "", None, None, None)
    content = _Content(subject, html_body, text_body, headers, None, None)
    raw = _raw_message(envelope, content, text_cte="8bit").as_bytes()
    template_strings = [source, reply_to or "", subject, html_body, text_body, TO_SENTINEL]
    template_strings.extend(headers.keys())
    template_strings.extend(headers.values())
    for sentinel, sentinel_bytes in zip(_SENTINELS, _SENTINEL_BYTES, strict=True):
        expected = sum(part.count(sentinel) for part in template_strings)
        actual = raw.count(sentinel_bytes)
        if actual != expected:
            raise MimeTemplateError(
                f"sentinel {sentinel!r} split by encoding: found {actual} intact copies, expected {expected}"
            )
    return CampaignMimeTemplate(raw=raw)


def render_campaign_template(template, *, to_email, tracking_token, unsubscribe_token):
    """Substitute per-recipient values into a built template; return raw bytes.

    Raises MimeTemplateError (never sends a half-substituted message) when a
    value is empty, carries CR/LF, contains a sentinel (which a later replace
    would corrupt), or any sentinel survives rendering.
    """
    values = (
        ("to_email", to_email),
        ("tracking_token", tracking_token),
        ("unsubscribe_token", unsubscribe_token),
    )
    for label, value in values:
        if not value or "\r" in value or "\n" in value:
            raise MimeTemplateError(f"unsafe per-recipient value for {label}")
        for sentinel in _SENTINELS:
            if sentinel in value:
                raise MimeTemplateError(f"per-recipient value for {label} contains a template sentinel")
    raw = template.raw
    raw = raw.replace(_SENTINEL_BYTES[0], tracking_token.encode("utf-8"))
    raw = raw.replace(_SENTINEL_BYTES[1], unsubscribe_token.encode("utf-8"))
    raw = raw.replace(_SENTINEL_BYTES[2], to_email.encode("utf-8"))
    for sentinel in _SENTINEL_BYTES:
        if sentinel in raw:
            raise MimeTemplateError("unsubstituted sentinel remains after rendering")
    return raw
