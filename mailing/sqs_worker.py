import logging
import queue
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from time import sleep

from mailing.aws import sqs_client
from mailing.db_retry import drop_unusable_connections
from mailing.sqs import records_from_messages

logger = logging.getLogger(__name__)

_DEFAULT_WRITER_BATCH_MAX_RECORDS = 100
_DEFAULT_WRITER_LINGER_SECONDS = 0.02


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
    def __init__(
        self,
        config,
        *,
        client=None,
        client_factory=None,
        batch_size=10,
        wait_time=20,
        visibility_timeout=None,
        concurrency=1,
        writer_batch_max_records=_DEFAULT_WRITER_BATCH_MAX_RECORDS,
    ):
        self.config = config
        self._explicit_client = client
        self._client_factory = client_factory or sqs_client
        self.client = client or self._client_factory()
        self.batch_size = batch_size
        self.wait_time = wait_time
        self.visibility_timeout = visibility_timeout
        self.concurrency = max(1, int(concurrency))
        self.writer_batch_max_records = max(1, int(writer_batch_max_records))

    def _validate_handler_response(self, response, messages):
        if not isinstance(response, dict):
            logger.error(
                "SQS worker handler returned non-dict response for queue %s: %r",
                self.config.queue_url,
                response,
            )
            return None

        failures = response.get("batchItemFailures")
        if not isinstance(failures, list):
            logger.error(
                "SQS worker handler returned invalid batchItemFailures for queue %s: %r",
                self.config.queue_url,
                failures,
            )
            return None

        received_ids = {message["MessageId"] for message in messages}
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("itemIdentifier"), str)
            or item["itemIdentifier"] not in received_ids
            for item in failures
        ):
            logger.error("SQS worker returned uncorrelated batch failures for queue %s", self.config.queue_url)
            return None
        return {item["itemIdentifier"] for item in failures}

    def run_once(self):
        messages = self._receive_messages()
        if not messages:
            return WorkerResult(received=0, deleted=0, failed=0)

        try:
            response = self.config.handler(records_from_messages(messages), None)
        except Exception:
            logger.exception("SQS worker handler failed for queue %s", self.config.queue_url)
            return WorkerResult(received=len(messages), deleted=0, failed=len(messages))

        failed_message_ids = self._validate_handler_response(response, messages)
        if failed_message_ids is None:
            return WorkerResult(received=len(messages), deleted=0, failed=len(messages))

        to_delete = [message for message in messages if message["MessageId"] not in failed_message_ids]
        deleted, batch_failed = self._delete_batch(to_delete)

        return WorkerResult(
            received=len(messages),
            deleted=deleted,
            failed=len(failed_message_ids) + batch_failed,
        )

    def _delete_batch(self, messages, *, client=None):
        """Delete ready messages with batch calls (SQS allows 10 entries each).

        Returns (deleted, batch_failed). Entries SQS reports as failed stay
        visible for retry/DLQ and count as failed, never as deleted.
        Falls back to per-message deletes when the client has no
        delete_message_batch (e.g. minimal test doubles).
        """
        if not messages:
            return 0, 0
        sqs = client or self.client
        delete_batch = getattr(sqs, "delete_message_batch", None)
        if delete_batch is None:
            deleted = 0
            for message in messages:
                try:
                    sqs.delete_message(
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
        if self.concurrency <= 1:
            while not should_stop():
                # A long-lived drain never sees Django's request signals: drop a
                # connection that broke (e.g. Postgres moved to a new IP) or aged
                # out, so the next enqueue reconnects through fresh DNS instead of
                # failing every batch on a dead socket.
                drop_unusable_connections()
                result = self.run_once()
                yield result
                if result.received == 0 and idle_sleep:
                    sleep(idle_sleep)
            return

        yield from self._run_forever_concurrent(should_stop=should_stop, idle_sleep=idle_sleep)

    def _run_forever_concurrent(self, *, should_stop, idle_sleep=0):
        writer_queue: queue.Queue = queue.Queue()
        results_queue: queue.Queue = queue.Queue()

        def writer_loop():
            while True:
                first = writer_queue.get()
                if first is None:
                    return
                pending = [first]
                total_messages = len(first[0])
                while total_messages < self.writer_batch_max_records:
                    try:
                        nxt = writer_queue.get(timeout=_DEFAULT_WRITER_LINGER_SECONDS)
                    except queue.Empty:
                        break
                    if nxt is None:
                        writer_queue.put(None)
                        break
                    pending.append(nxt)
                    total_messages += len(nxt[0])

                combined_messages = [msg for msgs, _fut in pending for msg in msgs]
                try:
                    drop_unusable_connections()
                    response = self.config.handler(records_from_messages(combined_messages), None)
                    failed_ids = self._validate_handler_response(response, combined_messages)
                except Exception as exc:
                    logger.exception("SQS worker handler failed for queue %s", self.config.queue_url)
                    for _msgs, fut in pending:
                        fut.set_exception(exc)
                    continue

                if failed_ids is None:
                    err = RuntimeError("SQS worker handler returned invalid response")
                    for _msgs, fut in pending:
                        fut.set_exception(err)
                    continue

                for msgs, fut in pending:
                    batch_ids = {m["MessageId"] for m in msgs}
                    fut.set_result(failed_ids & batch_ids)

        def poller_loop():
            thread_client = self._explicit_client or self._client_factory()
            while not should_stop():
                try:
                    messages = self._receive_messages(client=thread_client)
                except Exception:
                    logger.exception("SQS receive_message failed for queue %s", self.config.queue_url)
                    results_queue.put(WorkerResult(received=0, deleted=0, failed=0))
                    if idle_sleep:
                        sleep(idle_sleep)
                    continue
                if not messages:
                    results_queue.put(WorkerResult(received=0, deleted=0, failed=0))
                    if idle_sleep:
                        sleep(idle_sleep)
                    continue

                fut: Future = Future()
                writer_queue.put((messages, fut))
                try:
                    failed_message_ids = fut.result()
                except Exception:
                    results_queue.put(
                        WorkerResult(received=len(messages), deleted=0, failed=len(messages))
                    )
                    continue

                to_delete = [m for m in messages if m["MessageId"] not in failed_message_ids]
                deleted, batch_failed = self._delete_batch(to_delete, client=thread_client)
                results_queue.put(
                    WorkerResult(
                        received=len(messages),
                        deleted=deleted,
                        failed=len(failed_message_ids) + batch_failed,
                    )
                )

        writer_thread = threading.Thread(target=writer_loop, name=f"{self.config.name}-writer", daemon=True)
        writer_thread.start()
        pollers = [
            threading.Thread(target=poller_loop, name=f"{self.config.name}-poller-{i}", daemon=True)
            for i in range(self.concurrency)
        ]
        for t in pollers:
            t.start()

        try:
            while any(t.is_alive() for t in pollers) or not results_queue.empty():
                try:
                    yield results_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
        finally:
            for t in pollers:
                t.join(timeout=30)
            writer_queue.put(None)
            writer_thread.join(timeout=30)

    def _receive_messages(self, *, client=None):
        kwargs = {
            "QueueUrl": self.config.queue_url,
            "MaxNumberOfMessages": self.batch_size,
            "WaitTimeSeconds": self.wait_time,
            "MessageAttributeNames": ["All"],
            "AttributeNames": ["All"],
        }
        if self.visibility_timeout is not None:
            kwargs["VisibilityTimeout"] = self.visibility_timeout
        sqs = client or self.client
        response = sqs.receive_message(**kwargs)
        return response.get("Messages", [])
