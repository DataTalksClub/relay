"""Signing helpers for recipient-specific click destinations."""

import hashlib
import hmac

from django.conf import settings


def sign_click_redirect(tracking_token, destination_url):
    message = b"relay-click-url:v1\0" + tracking_token.encode("utf-8") + b"\0" + destination_url.encode("utf-8")
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_click_redirect(tracking_token, destination_url, signature):
    if not isinstance(signature, str) or len(signature) != 64:
        return False
    expected = sign_click_redirect(tracking_token, destination_url)
    return hmac.compare_digest(expected, signature)
