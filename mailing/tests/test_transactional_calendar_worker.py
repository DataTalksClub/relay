"""Worker transport observability tests for the optional calendar slice.

The durable queue payload drives the existing SQS handler into the shared SES
transport (mocked provider): the omitted-calendar simple send_email path, the
headers/parts send_raw_email path, and the corrupted-profile refusal together
with the positive control that proves the no-call assertion is genuine
evidence rather than a vacuous stub.
"""

from email import message_from_bytes

import boto3
import pytest
from botocore.stub import Stubber
from django.test import override_settings

from mailing.models import TransactionalMessageStatus
from mailing.services.transactional import build_transactional_queue_payload
from mailing.tests.transactional_calendar_helpers import (
    CALENDAR_CONTENT,
    FakeRawSesClient,
    RecordingSesClient,
    _event,
    simple_send_expected_params,
)

# Shared fixtures re-exported under their own names for pytest collection.
from mailing.tests.transactional_calendar_helpers import (
    api_client_record as api_client_record,
)
from mailing.tests.transactional_calendar_helpers import (
    contact as contact,
)
from mailing.tests.transactional_calendar_helpers import (
    organization as organization,
)
from mailing.tests.transactional_calendar_helpers import (
    template as template,
)
from mailing.tests.transactional_calendar_helpers import (
    transactional_message as transactional_message,
)
from mailing.workers import transactional_email_handler

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(DEFAULT_FROM_EMAIL="sender@example.com", AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="")
def test_worker_without_calendar_still_uses_simple_send_email(transactional_message, monkeypatch):
    ses = boto3.client(
        "ses",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    with Stubber(ses) as stubber:
        stubber.add_response("send_email", {"MessageId": "ses-message-123"}, simple_send_expected_params())
        monkeypatch.setattr("mailing.services.transactional_sender.ses_client_for_source", lambda source: ses)

        response = transactional_email_handler(
            _event("message-1", build_transactional_queue_payload(transactional_message))
        )

    transactional_message.refresh_from_db()
    assert response == {"batchItemFailures": []}
    assert transactional_message.status == TransactionalMessageStatus.SENT
    assert transactional_message.ses_message_id == "ses-message-123"


@override_settings(DEFAULT_FROM_EMAIL="sender@example.com", AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="")
def test_worker_with_headers_and_parts_without_calendar_still_uses_send_raw_email(
    transactional_message,
    monkeypatch,
):
    transactional_message.metadata = {
        "headers": {"X-Track": "keep"},
        "message_parts": [
            {
                "content_type": "text/plain",
                "content": "attached note",
                "filename": "note.txt",
                "disposition": "attachment",
            }
        ],
    }
    transactional_message.save(update_fields=["metadata", "updated_at"])
    ses = FakeRawSesClient()
    monkeypatch.setattr("mailing.services.transactional_sender.ses_client_for_source", lambda source: ses)

    response = transactional_email_handler(
        _event("message-1", build_transactional_queue_payload(transactional_message))
    )

    transactional_message.refresh_from_db()
    assert response == {"batchItemFailures": []}
    assert transactional_message.status == TransactionalMessageStatus.SENT
    parsed = message_from_bytes(ses.raw_params["RawMessage"]["Data"])
    assert parsed.get_content_type() == "multipart/mixed"


@override_settings(DEFAULT_FROM_EMAIL="sender@example.com", AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="")
def test_worker_refuses_corrupted_calendar_profile_before_provider_call(transactional_message, monkeypatch):
    transactional_message.metadata = {
        # Simulate a corrupted durable profile: the declared method no longer
        # matches the stored content, so the transport must refuse to build.
        "calendar_alternative": {"content": CALENDAR_CONTENT, "method": "CANCEL", "name": "event.ics"},
    }
    transactional_message.save(update_fields=["metadata", "updated_at"])
    recorded = RecordingSesClient()
    monkeypatch.setattr("mailing.services.transactional_sender.ses_client_for_source", lambda source: recorded)

    response = transactional_email_handler(
        _event("message-1", build_transactional_queue_payload(transactional_message))
    )

    transactional_message.refresh_from_db()
    assert response == {"batchItemFailures": [{"itemIdentifier": "message-1"}]}
    assert transactional_message.status == TransactionalMessageStatus.SENDING
    # No SES method may be invoked for a corrupted profile; the positive
    # control below proves this recording would observe a real send.
    assert recorded.calls == []


@override_settings(DEFAULT_FROM_EMAIL="sender@example.com", AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="")
def test_worker_with_valid_calendar_profile_records_provider_send_raw_email(transactional_message, monkeypatch):
    """Positive control for the corrupted-profile refusal test above.

    The same recording client wiring observes the real provider call when the
    durable profile is valid, so an empty call record in the refusal test
    genuinely means no SES method was invoked, not a vacuous stub.
    """
    transactional_message.metadata = {
        "calendar_alternative": {"content": CALENDAR_CONTENT, "method": "REQUEST", "name": "event.ics"},
    }
    transactional_message.save(update_fields=["metadata", "updated_at"])
    recorded = RecordingSesClient()
    monkeypatch.setattr("mailing.services.transactional_sender.ses_client_for_source", lambda source: recorded)

    response = transactional_email_handler(
        _event("message-1", build_transactional_queue_payload(transactional_message))
    )

    transactional_message.refresh_from_db()
    assert response == {"batchItemFailures": []}
    assert transactional_message.status == TransactionalMessageStatus.SENT
    assert [name for name, _params in recorded.calls] == ["send_raw_email"]
