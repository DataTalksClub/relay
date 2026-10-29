"""One-click "Block this sender" links in Relay-forwarded mail.

The person reading a forward is not signed in to Relay, so the link itself is
the credential: a signed, expiring token naming the forwarded message, the
value and the scope. Opening a link only renders a confirmation page; the rule
is created by the POST from that page, so mail scanners and link prefetchers
that GET every URL cannot block anyone by accident.

Two guards hold regardless of what a token says:

- A shared mailbox provider's domain is never blocked, only the address. One
  pitch from a gmail.com address must not silence every gmail.com sender.
- Our own domains are never blocked, at either scope, so a spoofed From cannot
  trick a click into dropping our own mail.
"""

import html
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from urllib.parse import urlsplit

from django.conf import settings
from django.core import signing
from django.urls import reverse

from mailing.models import BlockedSender, Client, InboundDomain, InboundMessage, SendingDomain
from mailing.services.inbound_views import address_domain, block_sender, normalize_address

BLOCK_ORIGIN_EMAIL_LINK = "email_link"
BLOCK_SALT = "mailing.inbound.block_link"
UNBLOCK_SALT = "mailing.inbound.unblock_link"
LINK_MAX_AGE_SECONDS = 60 * 60 * 24 * 30

# Domains where many unrelated people have addresses. Blocking one of these
# would block strangers, so only the exact address can be blocked there.
FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com",
    "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "msn.com", "outlook.de", "hotmail.de",
    "yahoo.com", "yahoo.co.uk", "yahoo.de", "yahoo.fr", "ymail.com", "rocketmail.com",
    "icloud.com", "me.com", "mac.com",
    "proton.me", "protonmail.com", "protonmail.ch", "pm.me",
    "aol.com", "gmx.com", "gmx.net", "gmx.de", "web.de", "t-online.de", "mail.com",
    "zoho.com", "fastmail.com", "hey.com", "tutanota.com", "tuta.io",
    "yandex.com", "yandex.ru", "mail.ru", "qq.com", "163.com", "126.com",
})
# Ours even before any row names them; the database adds the rest.
OWN_DOMAINS = frozenset({"datatalks.club", "aishippinglabs.com", "dtcdev.click"})


def own_domains():
    domains = set(OWN_DOMAINS)
    domains.update(InboundDomain.objects.values_list("name", flat=True))
    domains.update(SendingDomain.objects.values_list("name", flat=True))
    for address in Client.objects.exclude(default_from_email="").values_list("default_from_email", flat=True):
        domains.add(address_domain(address))
    domains.add(address_domain(settings.DEFAULT_FROM_EMAIL))
    return {domain.lower() for domain in domains if domain}


def _within(domain, domains):
    return any(domain == d or domain.endswith("." + d) for d in domains)


def refusal(scope, value):
    """Why (scope, value) cannot be blocked from a link, or "" when it can."""
    value = normalize_address(value)
    domain = address_domain(value) if scope == BlockedSender.Scope.ADDRESS else value
    if not domain or (scope == BlockedSender.Scope.ADDRESS and "@" not in value):
        return "invalid"
    if _within(domain, own_domains()):
        return "own_domain"
    if scope == BlockedSender.Scope.DOMAIN and _within(domain, FREEMAIL_DOMAINS):
        return "shared_domain"
    return ""


def issue_token(*, message_id, scope, value, salt=BLOCK_SALT):
    payload = {"m": message_id, "s": str(scope), "v": normalize_address(value)}
    return signing.TimestampSigner(salt=salt).sign_object(payload, compress=True)


def read_token(token, *, salt=BLOCK_SALT):
    """The signed payload, or None for anything malformed, tampered or expired."""
    try:
        payload = signing.TimestampSigner(salt=salt).unsign_object(token, max_age=LINK_MAX_AGE_SECONDS)
    except (signing.BadSignature, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"m", "s", "v"}
        or not isinstance(payload["m"], int)
        or payload["s"] not in BlockedSender.Scope.values
        or not isinstance(payload["v"], str)
    ):
        return None
    return payload


def link_base_url():
    # The console host serves Django for every path, while PUBLIC_BASE_URL can
    # be a site that only proxies the tracking and unsubscribe paths.
    if settings.INBOUND_BLOCK_LINK_BASE_URL:
        return settings.INBOUND_BLOCK_LINK_BASE_URL
    callback = urlsplit(settings.AUTH_CALLBACK_URL)
    if callback.scheme == "https" and callback.netloc:
        return f"{callback.scheme}://{callback.netloc}"
    return settings.PUBLIC_BASE_URL


def block_url(*, message_id, scope, value):
    token = issue_token(message_id=message_id, scope=scope, value=value)
    return link_base_url() + reverse("mailing:inbound_block_link", args=[token])


def unblock_url(*, message_id, scope, value):
    token = issue_token(message_id=message_id, scope=scope, value=value, salt=UNBLOCK_SALT)
    return link_base_url() + reverse("mailing:inbound_unblock_link", args=[token])


def sender_to_block(raw):
    """The original sender: the Reply-To address, else the From address."""
    headers = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    for name in ("Reply-To", "From"):
        addresses = [addr.lower() for _, addr in getaddresses([str(headers.get(name, ""))]) if "@" in addr]
        if addresses:
            return addresses[0]
    return ""


def forward_footer(inbound, raw):
    """(text, html) footer for a forward of `inbound`, or None when nothing is blockable."""
    address = sender_to_block(raw)
    if not address or refusal(BlockedSender.Scope.ADDRESS, address):
        return None
    links = [(f"Block this sender ({address})", block_url(message_id=inbound.pk, scope="address", value=address))]
    domain = address_domain(address)
    if not refusal(BlockedSender.Scope.DOMAIN, domain):
        links.append((f"Block domain {domain}", block_url(message_id=inbound.pk, scope="domain", value=domain)))
    text = "\n\n-- \nForwarded by Relay. Unwanted mail?\n" + "\n".join(f"{label}: {url}" for label, url in links) + "\n"
    anchors = " &middot; ".join(
        f'<a href="{html.escape(url)}" style="color:#555">{html.escape(label)}</a>' for label, url in links
    )
    markup = (
        '<div style="margin-top:24px;padding-top:8px;border-top:1px solid #ddd;'
        f'font:12px/1.5 sans-serif;color:#777">Forwarded by Relay. Unwanted mail? {anchors}</div>'
    )
    return text, markup


def apply_block(payload):
    """Create the rule (or find the existing one). Returns (rule, created, refusal)."""
    why = refusal(payload["s"], payload["v"])
    if why:
        return None, False, why
    message = InboundMessage.objects.filter(pk=payload["m"]).first()
    reason = message.message_id if message else f"inbound message {payload['m']}"
    rule, created = block_sender(
        message=message, scope=payload["s"], value=payload["v"], reason=reason, origin=BLOCK_ORIGIN_EMAIL_LINK,
    )
    return rule, created, ""


def apply_unblock(payload):
    """Delete the rule for (scope, value). Returns how many rules were removed."""
    deleted, _ = BlockedSender.objects.filter(scope=payload["s"], value=payload["v"]).delete()
    return deleted
