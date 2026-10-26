import json
import logging

from mailing.sqs_worker import SqsWorker, WorkerConfig


class FakeSqsClient:
    def __init__(self, messages, *, fail_batch_ids=()):
        self.messages = messages
        self.receive_calls = []
        self.deleted_receipts = []
        self.batch_calls = []
        self.fail_batch_ids = set(fail_batch_ids)

    def receive_message(self, **kwargs):
        self.receive_calls.append(kwargs)
        return {"Messages": self.messages}

    def delete_message(self, **kwargs):
        self.deleted_receipts.append(kwargs["ReceiptHandle"])

    def delete_message_batch(self, *, QueueUrl, Entries):
        self.batch_calls.append({"QueueUrl": QueueUrl, "Entries": Entries})
        successful, failed = [], []
        for entry in Entries:
            if entry["Id"] in self.fail_batch_ids:
                failed.append({"Id": entry["Id"], "Code": "ReceiptHandleIsInvalid", "Message": "stale receipt"})
            else:
                successful.append({"Id": entry["Id"]})
        return {"Successful": successful, "Failed": failed}

    @property
    def batch_deleted_receipts(self):
        receipts_by_id = {
            entry["Id"]: entry["ReceiptHandle"]
            for call in self.batch_calls
            for entry in call["Entries"]
            if entry["Id"] not in self.fail_batch_ids
        }
        return [receipts_by_id[entry["Id"]] for call in self.batch_calls for entry in call["Entries"]
                if entry["Id"] in receipts_by_id]


class LegacyFakeSqsClient:
    """Minimal double without delete_message_batch to cover the fallback path."""

    def __init__(self, messages):
        self.messages = messages
        self.deleted_receipts = []

    def receive_message(self, **kwargs):
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
    assert client.batch_deleted_receipts == ["success-1-receipt", "success-2-receipt"]
    assert client.deleted_receipts == [], "batch path must not fall back to single deletes"
    assert len(client.batch_calls) == 1
    assert client.batch_calls[0]["QueueUrl"] == "https://sqs.example/test"
    assert [entry["ReceiptHandle"] for entry in client.batch_calls[0]["Entries"]] == [
        "success-1-receipt",
        "success-2-receipt",
    ]
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
    assert client.batch_calls == []


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
    assert client.batch_calls == []


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
        assert client.batch_calls == []


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
        assert client.batch_calls == []


def test_sqs_worker_counts_partial_batch_delete_failure():
    messages = [_message("ok-1"), _message("stale-1")]
    client = FakeSqsClient(messages, fail_batch_ids={"msg-1"})

    worker = SqsWorker(
        WorkerConfig(name="test", queue_url="https://sqs.example/test", handler=lambda e, c: {"batchItemFailures": []}),
        client=client,
    )
    result = worker.run_once()

    assert result.received == 2
    assert result.deleted == 1
    assert result.failed == 1
    assert client.batch_deleted_receipts == ["ok-1-receipt"]
    assert len(client.batch_calls) == 1


def test_sqs_worker_falls_back_to_single_deletes_without_batch_api():
    client = LegacyFakeSqsClient([_message("success-1"), _message("success-2")])
    worker = SqsWorker(
        WorkerConfig(name="test", queue_url="https://sqs.example/test", handler=lambda e, c: {"batchItemFailures": []}),
        client=client,
    )
    result = worker.run_once()

    assert result.received == 2
    assert result.deleted == 2
    assert result.failed == 0
    assert client.deleted_receipts == ["success-1-receipt", "success-2-receipt"]


def test_sqs_worker_counts_batch_delete_exception_as_failures():
    class ExplodingBatchClient(FakeSqsClient):
        def delete_message_batch(self, **kwargs):
            raise RuntimeError("sqs unavailable")

    client = ExplodingBatchClient([_message("m-1"), _message("m-2")])
    worker = SqsWorker(
        WorkerConfig(name="test", queue_url="https://sqs.example/test", handler=lambda e, c: {"batchItemFailures": []}),
        client=client,
    )
    result = worker.run_once()

    assert result.received == 2
    assert result.deleted == 0
    assert result.failed == 2


def test_sqs_worker_fallback_delete_error_keeps_remaining_visible():
    class FlakyLegacyClient(LegacyFakeSqsClient):
        def delete_message(self, **kwargs):
            if kwargs["ReceiptHandle"] == "fail-1-receipt":
                raise RuntimeError("stale receipt")
            self.deleted_receipts.append(kwargs["ReceiptHandle"])

    client = FlakyLegacyClient([_message("fail-1"), _message("ok-2")])
    worker = SqsWorker(
        WorkerConfig(name="test", queue_url="https://sqs.example/test", handler=lambda e, c: {"batchItemFailures": []}),
        client=client,
    )
    result = worker.run_once()

    assert result.received == 2
    assert result.deleted == 0
    assert result.failed == 2


def test_sqs_worker_chunks_batches_over_ten_messages():
    messages = [_message(f"m-{index}") for index in range(11)]
    client = FakeSqsClient(messages)
    worker = SqsWorker(
        WorkerConfig(name="test", queue_url="https://sqs.example/test", handler=lambda e, c: {"batchItemFailures": []}),
        client=client,
    )
    result = worker.run_once()

    assert result.received == 11
    assert result.deleted == 11
    assert result.failed == 0
    assert len(client.batch_calls) == 2
    assert len(client.batch_calls[0]["Entries"]) == 10
    assert len(client.batch_calls[1]["Entries"]) == 1
    assert [entry["Id"] for entry in client.batch_calls[1]["Entries"]] == ["msg-10"]


def test_sqs_worker_counts_unacknowledged_entries_as_failures(caplog):
    class SilentBatchClient(FakeSqsClient):
        def delete_message_batch(self, *, QueueUrl, Entries):
            self.batch_calls.append({"QueueUrl": QueueUrl, "Entries": Entries})
            return {"Successful": [], "Failed": []}

    messages = [_message("ghost-1")]
    client = SilentBatchClient(messages)
    worker = SqsWorker(
        WorkerConfig(name="test", queue_url="https://sqs.example/test", handler=lambda e, c: {"batchItemFailures": []}),
        client=client,
    )
    with caplog.at_level(logging.WARNING, logger="mailing.sqs_worker"):
        result = worker.run_once()

    assert result.received == 1
    assert result.deleted == 0
    assert result.failed == 1
    assert "unacknowledged" in caplog.text
