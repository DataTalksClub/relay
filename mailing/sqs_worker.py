import logging
from dataclasses import dataclass
from time import sleep

from mailing.aws import sqs_client
from mailing.sqs import records_from_messages

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerConfig:
    name: str
    queue_url: str
    handler: object


@dataclass(frozen=True)
class WorkerResult:
    received: int
    deleted: int
    failed: int


class SqsWorker:
    def __init__(self, config, *, client=None, batch_size=10, wait_time=20, visibility_timeout=None):
        self.config = config
        self.client = client or sqs_client()
        self.batch_size = batch_size
        self.wait_time = wait_time
        self.visibility_timeout = visibility_timeout

    def run_once(self):
        messages = self._receive_messages()
        if not messages:
            return WorkerResult(received=0, deleted=0, failed=0)

        try:
            response = self.config.handler(records_from_messages(messages), None)
        except Exception:
            logger.exception("SQS worker handler failed for queue %s", self.config.queue_url)
            return WorkerResult(received=len(messages), deleted=0, failed=len(messages))

        if not isinstance(response, dict):
            logger.error(
                "SQS worker handler returned non-dict response for queue %s: %r",
                self.config.queue_url,
                response,
            )
            return WorkerResult(received=len(messages), deleted=0, failed=len(messages))

        failures = response.get("batchItemFailures")
        if not isinstance(failures, list):
            logger.error(
                "SQS worker handler returned invalid batchItemFailures for queue %s: %r",
                self.config.queue_url,
                failures,
            )
            return WorkerResult(received=len(messages), deleted=0, failed=len(messages))

        received_ids = {message["MessageId"] for message in messages}
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("itemIdentifier"), str)
            or item["itemIdentifier"] not in received_ids
            for item in failures
        ):
            logger.error("SQS worker returned uncorrelated batch failures for queue %s", self.config.queue_url)
            return WorkerResult(received=len(messages), deleted=0, failed=len(messages))
        failed_message_ids = {item["itemIdentifier"] for item in failures}

        to_delete = [message for message in messages if message["MessageId"] not in failed_message_ids]
        deleted, batch_failed = self._delete_batch(to_delete)

        return WorkerResult(
            received=len(messages),
            deleted=deleted,
            failed=len(failed_message_ids) + batch_failed,
        )

    def _delete_batch(self, messages):
        """Delete ready messages with batch calls (SQS allows 10 entries each).

        Returns (deleted, batch_failed). Entries SQS reports as failed stay
        visible for retry/DLQ and count as failed, never as deleted.
        Falls back to per-message deletes when the client has no
        delete_message_batch (e.g. minimal test doubles).
        """
        if not messages:
            return 0, 0
        delete_batch = getattr(self.client, "delete_message_batch", None)
        if delete_batch is None:
            deleted = 0
            for message in messages:
                try:
                    self.client.delete_message(
                        QueueUrl=self.config.queue_url,
                        ReceiptHandle=message["ReceiptHandle"],
                    )
                except Exception:
                    logger.exception(
                        "SQS delete failed for queue %s message %s",
                        self.config.queue_url,
                        message["MessageId"],
                    )
                    return deleted, len(messages) - deleted
                deleted += 1
            return deleted, 0
        deleted_total = 0
        failed_total = 0
        for chunk_start in range(0, len(messages), 10):
            chunk = messages[chunk_start : chunk_start + 10]
            deleted, failed = self._delete_chunk(delete_batch, chunk, offset=chunk_start)
            deleted_total += deleted
            failed_total += failed
        return deleted_total, failed_total

    def _delete_chunk(self, delete_batch, messages, *, offset=0):
        # NOTE: a message deleted here was already processed by the handler;
        # a failed delete leaves it visible and it will be processed AGAIN.
        # Handler results must therefore be idempotent (SES ingress dedupes
        # on provider_event_id).
        entries = [
            {"Id": f"msg-{offset + index}", "ReceiptHandle": message["ReceiptHandle"]}
            for index, message in enumerate(messages)
        ]
        id_by_entry = {entry["Id"]: message["MessageId"] for entry, message in zip(entries, messages)}
        try:
            response = delete_batch(QueueUrl=self.config.queue_url, Entries=entries) or {}
        except Exception:
            logger.exception("SQS batch delete failed for queue %s", self.config.queue_url)
            return 0, len(messages)
        successful = response.get("Successful", []) or []
        failed_entries = response.get("Failed", []) or []
        successful_ids = set()
        for item in successful:
            entry_id = item.get("Id") if isinstance(item, dict) else None
            if isinstance(entry_id, str) and entry_id in id_by_entry:
                successful_ids.add(entry_id)
        seen_ids = set(successful_ids)
        for item in failed_entries:
            entry_id = item.get("Id") if isinstance(item, dict) else None
            if isinstance(entry_id, str) and entry_id in id_by_entry:
                seen_ids.add(entry_id)
            message_id = id_by_entry.get(entry_id, entry_id)
            logger.warning(
                "SQS batch delete entry failed for queue %s message %s: %r",
                self.config.queue_url,
                message_id,
                item,
            )
        unacknowledged = [entry_id for entry_id in id_by_entry if entry_id not in seen_ids]
        if unacknowledged:
            logger.warning(
                "SQS batch delete for queue %s left %d of %d entries unacknowledged; keeping them visible",
                self.config.queue_url,
                len(unacknowledged),
                len(messages),
            )
        batch_failed = len(messages) - len(successful_ids)
        return len(successful_ids), batch_failed

    def run_forever(self, *, should_stop, idle_sleep=0):
        while not should_stop():
            result = self.run_once()
            yield result
            if result.received == 0 and idle_sleep:
                sleep(idle_sleep)

    def _receive_messages(self):
        kwargs = {
            "QueueUrl": self.config.queue_url,
            "MaxNumberOfMessages": self.batch_size,
            "WaitTimeSeconds": self.wait_time,
            "MessageAttributeNames": ["All"],
            "AttributeNames": ["All"],
        }
        if self.visibility_timeout is not None:
            kwargs["VisibilityTimeout"] = self.visibility_timeout
        response = self.client.receive_message(**kwargs)
        return response.get("Messages", [])
