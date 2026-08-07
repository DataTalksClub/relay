"""Every task's return value must survive the task backend.

`django-tasks-db` stores a task's return value in a JSONField. A task that
returns a model instance or a dataclass therefore raises *after* its work has
been committed, which is the worst shape of failure available: the email is
sent, and the task row says it failed.

This was not hypothetical. `send_transactional_email` returned a
`TransactionalMessage`, and on the sandbox host every transactional send was
recorded as failed while the mail went out normally. The existing suite missed
it because the one task it exercised end to end, `process_email_event`,
returns None -- the only return type that happens to be JSON-safe.

Each case below feeds the task the object type its service really returns, and
asserts the task hands back something JSON can hold.
"""

import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from mailing import tasks
from mailing.services import ses_webhooks


@dataclass
class FakeCampaignResult:
    """Shaped like services.campaign_sender.CampaignSendResult, which is a
    dataclass -- also not JSON-serialisable by the backend."""

    sent_count: int = 3
    skipped_count: int = 1
    failed_count: int = 0


def _fake_message():
    """Stands in for a TransactionalMessage. A real model instance is what the
    service returns and what the backend chokes on."""
    return SimpleNamespace(pk=42, status="sent")


def _fake_event():
    return SimpleNamespace(pk=7, event_type="bounce")


CASES = [
    pytest.param(
        "send_transactional_email",
        "send_transactional_email_from_queue",
        "validate_transactional_email_message",
        _fake_message(),
        {"transactional_message_id": 42, "status": "sent"},
        id="transactional",
    ),
    pytest.param(
        "send_campaign_email_batch",
        "send_campaign_batch",
        "validate_campaign_email_message",
        FakeCampaignResult(),
        {"sent": 3, "skipped": 1, "failed": 0},
        id="campaign",
    ),
]


@pytest.mark.parametrize("task_name,service_name,validator_name,service_return,expected", CASES)
def test_task_return_value_is_json_serialisable(
    monkeypatch, task_name, service_name, validator_name, service_return, expected
):
    monkeypatch.setattr(tasks, validator_name, lambda payload: None)
    monkeypatch.setattr(tasks, service_name, lambda payload, **kwargs: service_return)

    result = getattr(tasks, task_name).func({})

    assert result == expected
    json.dumps(result)  # the property that actually matters


def test_ses_webhook_task_return_value_is_json_serialisable(monkeypatch):
    """Patched on the module the task imports from, because the task does that
    import inside its body to break a cycle -- so patching `tasks` would miss."""
    monkeypatch.setattr(ses_webhooks, "normalize_ses_webhook_worker_payload", lambda payload: payload)
    monkeypatch.setattr(ses_webhooks, "process_ses_webhook", lambda payload: _fake_event())

    result = tasks.process_ses_webhook_event.func({})

    assert result == {"email_event_id": 7, "event_type": "bounce"}
    json.dumps(result)


def test_summarise_passes_none_through():
    """Several services return None for 'nothing to do' -- a duplicate SES
    notification, an already-acknowledged message. That must stay None rather
    than become a summary of nothing."""
    assert tasks._summarise(None, lambda value: {"never": "called"}) is None
