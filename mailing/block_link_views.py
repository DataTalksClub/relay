"""Public pages behind the block links in forwarded mail.

No login: the signed token is the credential. GET only renders a page; the
rule is changed by the POST from that page, so link prefetchers are harmless.
"""

from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from mailing.services.block_links import (
    UNBLOCK_SALT,
    apply_block,
    apply_unblock,
    read_token,
    refusal,
    unblock_url,
)

TEMPLATE = "mailing/block_link.html"


def _page(request, status=200, **context):
    response = render(request, TEMPLATE, context=context, status=status)
    response["X-Robots-Tag"] = "noindex"
    response["Referrer-Policy"] = "no-referrer"
    return response


@csrf_exempt
@require_http_methods(["GET", "POST"])
def inbound_block_link(request, token):
    payload = read_token(token)
    if payload is None:
        return _page(request, status=404, invalid=True)
    target = {"scope": payload["s"], "value": payload["v"]}
    if request.method == "GET":
        why = refusal(payload["s"], payload["v"])
        return _page(request, status=400 if why else 200, refused=why, confirm_block=True, **target)
    rule, created, why = apply_block(payload)
    if why:
        return _page(request, status=400, refused=why, **target)
    return _page(
        request, blocked=True, created=created, unblock_link=unblock_url(
            message_id=payload["m"], scope=rule.scope, value=rule.value,
        ), **target,
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def inbound_unblock_link(request, token):
    payload = read_token(token, salt=UNBLOCK_SALT)
    if payload is None:
        return _page(request, status=404, invalid=True)
    target = {"scope": payload["s"], "value": payload["v"]}
    if request.method == "GET":
        return _page(request, confirm_unblock=True, **target)
    return _page(request, unblocked=True, removed=apply_unblock(payload), **target)
