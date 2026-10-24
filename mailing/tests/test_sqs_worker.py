import json

from mailing.sqs_worker import SqsWorker, WorkerConfig


class FakeSqsClient:
    def __init__(self, messages):
        self.messages = messages
        self.receive_calls = []
        self.deleted_receipts = []

    def receive_message(self, **kwargs):
        self.receive_calls.append(kwargs)
        return {"Messages": self.messages}

    def delete_message(self, **kwargs):
        self.deleted_receipts.append(kwargs["ReceiptHandle"])


def test_sqs_worker_deletes_successful_messages_only():
    messages = [
        _message("success-1"),
        _message("failed-1"),
        _message("success-2"),
    ]
    client = FakeSqsClient(messages)

    def handler(event, context):
        assert [record["messageId"] for record in event["Records"]] == ["success-1", "failed-1", "success-2"]
        return {"batchItemFailures": [{"itemIdentifier": "failed-1"}]}

    worker = SqsWorker(
        WorkerConfig(name="test", queue_url="https://sqs.example/test", handler=handler),
        client=client,
        batch_size=3,
        wait_time=7,
        visibility_timeout=30,
    )

    result = worker.run_once()

    assert result.received == 3
    assert result.deleted == 2
    assert result.failed == 1
    assert client.deleted_receipts == ["success-1-receipt", "success-2-receipt"]
    assert client.receive_calls == [
        {
            "QueueUrl": "https://sqs.example/test",
            "MaxNumberOfMessages": 3,
            "WaitTimeSeconds": 7,
            "MessageAttributeNames": ["All"],
            "AttributeNames": ["All"],
            "VisibilityTimeout": 30,
        }
    ]


def test_sqs_worker_empty_poll_does_not_delete():
    client = FakeSqsClient([])
    worker = SqsWorker(
        WorkerConfig(name="test", queue_url="https://sqs.example/test", handler=lambda event, context: None),
        client=client,
    )

    result = worker.run_once()

    assert result.received == 0
    assert result.deleted == 0
    assert result.failed == 0
    assert client.deleted_receipts == []


def test_sqs_worker_does_not_delete_on_handler_exception():
    client = FakeSqsClient([_message("err-1")])

    def exploding_handler(event, context):
        raise RuntimeError("database connection crashed")

    worker = SqsWorker(
        WorkerConfig(name="test", queue_url="https://sqs.example/test", handler=exploding_handler),
        client=client,
    )

    result = worker.run_once()

    assert result.received == 1
    assert result.deleted == 0
    assert result.failed == 1
    assert client.deleted_receipts == [], "message must be preserved for SQS retry and DLQ"


def test_sqs_worker_does_not_delete_on_invalid_or_non_dict_response():
    client = FakeSqsClient([_message("bad-resp-1")])

    for bad_response in (None, "error", 123, {"invalid_key": True}):
        worker = SqsWorker(
            WorkerConfig(name="test", queue_url="https://sqs.example/test", handler=lambda e, c: bad_response),
            client=client,
        )
        result = worker.run_once()
        assert result.received == 1
        assert result.deleted == 0
        assert result.failed == 1
        assert client.deleted_receipts == []


def _message(message_id, payload=None):
    return {
        "MessageId": message_id,
        "ReceiptHandle": f"{message_id}-receipt",
        "Body": json.dumps(payload or {"contract": "test"}),
        "MD5OfBody": "md5",
    }


def test_sqs_worker_preserves_entire_batch_for_uncorrelated_failures():
    for failures in ([{}], [None], [{"itemIdentifier": "unknown"}], [{"itemIdentifier": []}],
                     [{"itemIdentifier": "failed-1"}, {}]):
        client = FakeSqsClient([_message("failed-1"), _message("other-1")])
        worker = SqsWorker(
            WorkerConfig(name="test", queue_url="https://sqs.example/test",
                         handler=lambda e, c: {"batchItemFailures": failures}), client=client,
        )
        result = worker.run_once()
        assert result.deleted == 0
        assert result.failed == 2
        assert client.deleted_receipts == []
