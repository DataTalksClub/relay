"""Shared helpers for client callback tests.

Nothing here is collected by pytest: the module only exists so the callback
fixtures, the fake no-redirect opener, and the HTTP error builders stay
identical across every test file that exercises the dispatcher.
"""

from urllib.error import HTTPError

from mailing.models import CallbackEndpoint

DEFAULT_CALLBACK_URL = "https://callback.example.com/hooks"
DEFAULT_CALLBACK_SECRET = "callback-signing-secret"


class CallbackResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def create_callback_endpoint(client, *, url=DEFAULT_CALLBACK_URL, secret=DEFAULT_CALLBACK_SECRET):
    return CallbackEndpoint.objects.create(client=client, url=url, signing_secret=secret)


class FakeCallbackOpener:
    """Stands in for the dispatcher's no-redirect opener, recording requests.

    Outcomes are consumed one per request: an ``int`` answers with that HTTP
    status, an ``Exception`` instance is raised instead.
    """

    def __init__(self, outcomes=None):
        self.requests = []
        self.outcomes = list(outcomes or [])

    def open(self, request, *, timeout):
        self.requests.append(
            {
                "url": request.full_url,
                "body": bytes(request.data),
                "headers": {key.lower(): value for key, value in request.header_items()},
                "timeout": timeout,
            }
        )
        outcome = self.outcomes.pop(0) if self.outcomes else 200
        if isinstance(outcome, Exception):
            raise outcome
        return CallbackResponse(outcome)


def install_fake_opener(monkeypatch, outcomes=None):
    opener = FakeCallbackOpener(outcomes)
    monkeypatch.setattr("mailing.services.client_callbacks.NO_REDIRECT_OPENER", opener)
    return opener


def http_error(code):
    return HTTPError(DEFAULT_CALLBACK_URL, code, "error", hdrs=None, fp=None)
