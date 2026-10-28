import pytest

from mailing.sqs import json_body, partial_batch_response, process_sqs_event, records_from_messages


def test_partial_batch_response_uses_lambda_shape():
    response = partial_batch_response(["message-1", "message-2"])

    assert response == {
        "batchItemFailures": [
            {"itemIdentifier": "message-1"},
            {"itemIdentifier": "message-2"},
        ]
    }


def test_records_from_messages_builds_sqs_event_shape():
    event = records_from_messages(
        [
            {
                "MessageId": "message-1",
                "ReceiptHandle": "receipt-1",
                "Body": '{"type": "transactional"}',
                "MD5OfBody": "md5",
            }
        ]
    )

    assert event["Records"][0]["messageId"] == "message-1"
    assert event["Records"][0]["eventSource"] == "aws:sqs"
    assert json_body(event["Records"][0]) == {"type": "transactional"}


def test_process_sqs_event_passes_decoded_messages_to_handler():
    event = records_from_messages(
        [
            {
                "MessageId": "message-1",
                "ReceiptHandle": "receipt-1",
                "Body": '{"type": "transactional"}',
            }
        ]
    )
    processed = []

    response = process_sqs_event(event, lambda body, record: processed.append((body, record["messageId"])))

    assert processed == [({"type": "transactional"}, "message-1")]
    assert response == {"batchItemFailures": []}


def test_process_sqs_event_reports_failed_message_ids():
    event = records_from_messages(
        [
            {
                "MessageId": "message-1",
                "ReceiptHandle": "receipt-1",
                "Body": '{"type": "transactional"}',
            }
        ]
    )

    def handler(body, record):
        raise RuntimeError("transient failure")

    assert process_sqs_event(event, handler) == {
        "batchItemFailures": [{"itemIdentifier": "message-1"}],
    }


def test_process_sqs_event_logs_failure_reason_with_redacted_secrets_and_emails(caplog):
    event = records_from_messages(
        [
            {
                "MessageId": "msg-fail-42",
                "ReceiptHandle": "receipt-fail-42",
                "Body": '{"type": "transactional"}',
            }
        ]
    )

    def handler(body, record):
        raise ValueError("Failed sending to user.secret@example.com with token=supersecretkey123")

    with caplog.at_level("ERROR"):
        response = process_sqs_event(event, handler)

    assert response == {"batchItemFailures": [{"itemIdentifier": "msg-fail-42"}]}
    assert len(caplog.records) == 1
    log_text = caplog.text
    assert "msg-fail-42" in log_text
    assert "ValueError" in log_text
    assert "[redacted-email]" in log_text
    assert "token=[redacted]" in log_text
    assert "user.secret@example.com" not in log_text
    assert "supersecretkey123" not in log_text


def test_process_sqs_event_preserves_messages_on_unidentified_failure():
    event = {
        "Records": [
            {"messageId": "msg-1", "body": "{}"},
            {"messageId": "", "body": "{}"},  # Record with empty messageId
        ]
    }

    def handler(body, record):
        if not record.get("messageId"):
            raise ValueError("failure without messageId")

    response = process_sqs_event(event, handler)
    # When an unidentifiable failure occurs, all messages must be reported failed to avoid deleting
    assert response == {"batchItemFailures": [{"itemIdentifier": "msg-1"}]}


@pytest.mark.parametrize("reason", [
    "Authorization: Bearer synthetic-secret",
    "authorization=Basic synthetic-secret",
    '{"token": "synthetic-secret"}',
    "{'password': 'synthetic-secret with spaces'}",
    '{"private_key": "synthetic-secret\\nwith\\nlines"}',
    '{"secret": "synthetic-secret with \\"escaped\\" quotes"}',
    'api_key=synthetic-secret; retry later',
])
def test_failure_logs_redact_headers_and_quoted_secret_values(caplog, reason):
    event = {"Records": [{"messageId": "secret-failure", "body": "{}"}]}

    def fail(body, record):
        raise ValueError(f"Request failed: {reason}")

    with caplog.at_level("ERROR"):
        assert process_sqs_event(event, fail) == {
            "batchItemFailures": [{"itemIdentifier": "secret-failure"}],
        }
    assert "synthetic-secret" not in caplog.text
    assert "[redacted]" in caplog.text
    assert "Request failed" in caplog.text
    assert "secret-failure" in caplog.text
    assert "ValueError" in caplog.text
