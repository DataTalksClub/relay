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

        deleted = 0
        for message in messages:
            if message["MessageId"] in failed_message_ids:
                continue
            self.client.delete_message(
                QueueUrl=self.config.queue_url,
                ReceiptHandle=message["ReceiptHandle"],
            )
            deleted += 1

        return WorkerResult(
            received=len(messages),
            deleted=deleted,
            failed=len(failed_message_ids),
        )

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
