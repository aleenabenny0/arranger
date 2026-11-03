"""The job queue port and its adapters: in memory for tests and development, SQS for production.

The `jobs` table stays the record of truth: status, progress, attempts,
result. The queue carries one fact per message, "job <id> is ready", so a
worker can wait on a socket instead of polling the table, and workers on many
hosts can share one queue without sharing a database poller.

Delivery is at least once, so everything a worker does with a message is
idempotent by job id: the row is claimed with a conditional update (queued ->
running), a duplicate delivery finds it already claimed and is dropped, and
the message is deleted only after the row is in a final state. A message that
keeps failing is moved to a dead-letter queue instead of looping forever.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Protocol

DEFAULT_MAX_RECEIVES = 3
MAX_LONG_POLL_SECONDS = 20     # the most SQS allows per receive


@dataclass(frozen=True)
class QueuedMessage:
    """One delivery of a job. `attempt` counts deliveries, starting at 1."""

    receipt: str
    job_id: str
    attempt: int


class JobQueue(Protocol):
    """What the API and the worker need from a queue."""

    name: str
    max_receives: int

    def send(self, job_id: str) -> str: ...
    def receive(self, *, wait_seconds: int, visibility_seconds: int) -> QueuedMessage | None: ...
    def delete(self, message: QueuedMessage) -> None: ...
    def retry_later(self, message: QueuedMessage, delay_seconds: int) -> None: ...
    def dead_letter(self, message: QueuedMessage, reason: str) -> None: ...
    def healthy(self) -> bool: ...


# --- in memory ---------------------------------------------------------------


class InMemoryQueue:
    """A queue with SQS semantics inside one process.

    Messages received are invisible for `visibility_seconds`, then delivered
    again with a higher attempt count; a message received more than
    `max_receives` times is moved to `dead_letters` instead of delivered, as
    an SQS redrive policy would. `clock` is injectable so tests can move time.
    """

    name = "memory"

    def __init__(self, *, max_receives: int = DEFAULT_MAX_RECEIVES, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_receives = max(1, int(max_receives))
        self._clock = clock
        self._ready: deque[dict] = deque()
        self._in_flight: dict[str, tuple[dict, float]] = {}
        self.dead_letters: list[dict] = []
        self.sent: list[str] = []
        self._cv = threading.Condition()

    def send(self, job_id: str) -> str:
        message = {"id": uuid.uuid4().hex, "job_id": job_id, "receives": 0}
        with self._cv:
            self._ready.append(message)
            self.sent.append(job_id)
            self._cv.notify()
        return message["id"]

    def _release_expired(self, now: float) -> None:
        for receipt, (message, visible_at) in list(self._in_flight.items()):
            if visible_at <= now:
                del self._in_flight[receipt]
                self._ready.appendleft(message)

    def receive(self, *, wait_seconds: int = MAX_LONG_POLL_SECONDS, visibility_seconds: int = 60) -> QueuedMessage | None:
        deadline = time.monotonic() + max(0, wait_seconds)
        with self._cv:
            while True:
                now = self._clock()
                self._release_expired(now)
                while self._ready:
                    message = self._ready.popleft()
                    if message["receives"] >= self.max_receives:
                        self.dead_letters.append({**message, "reason": "too many deliveries"})
                        continue
                    message["receives"] += 1
                    receipt = uuid.uuid4().hex
                    self._in_flight[receipt] = (message, now + visibility_seconds)
                    return QueuedMessage(receipt, message["job_id"], message["receives"])
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(min(remaining, 0.05))

    def delete(self, message: QueuedMessage) -> None:
        with self._cv:
            self._in_flight.pop(message.receipt, None)

    def retry_later(self, message: QueuedMessage, delay_seconds: int) -> None:
        """Deliver the message again after `delay_seconds` instead of the full visibility timeout."""
        with self._cv:
            entry = self._in_flight.get(message.receipt)
            if entry is not None:
                self._in_flight[message.receipt] = (entry[0], self._clock() + max(0, delay_seconds))
                self._cv.notify_all()

    def dead_letter(self, message: QueuedMessage, reason: str) -> None:
        with self._cv:
            entry = self._in_flight.pop(message.receipt, None)
            if entry is not None:
                self.dead_letters.append({**entry[0], "reason": reason})

    def healthy(self) -> bool:
        return True

    # --- test helpers --------------------------------------------------------

    def expire_in_flight(self) -> None:
        """Make every in-flight message visible again, as a visibility timeout would."""
        with self._cv:
            for receipt, (message, _) in list(self._in_flight.items()):
                self._in_flight[receipt] = (message, float("-inf"))
            self._cv.notify_all()

    @property
    def pending(self) -> int:
        with self._cv:
            return len(self._ready)

    @property
    def in_flight(self) -> int:
        with self._cv:
            return len(self._in_flight)


# --- SQS ---------------------------------------------------------------------


class SqsQueue:
    """Amazon SQS (or LocalStack) through boto3. Standard queue, one message per job.

    `attempt` is SQS's ApproximateReceiveCount. The dead-letter queue is
    normally reached through the queue's redrive policy (see `ensure_queues`);
    `dead_letter` moves a message there explicitly for the cases the policy
    cannot see, such as a body that is not a job id.
    """

    name = "sqs"

    def __init__(
        self,
        queue_url: str,
        *,
        dead_letter_url: str = "",
        client: Any = None,
        endpoint_url: str = "",
        region: str = "",
        max_receives: int = DEFAULT_MAX_RECEIVES,
    ) -> None:
        if not queue_url:
            raise ValueError("SqsQueue needs a queue URL")
        self.queue_url = queue_url
        self.dead_letter_url = dead_letter_url
        self.max_receives = max(1, int(max_receives))
        self.client = client if client is not None else sqs_client(endpoint_url=endpoint_url, region=region)

    def send(self, job_id: str) -> str:
        response = self.client.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps({"job_id": job_id}))
        return str(response.get("MessageId", ""))

    def receive(self, *, wait_seconds: int = MAX_LONG_POLL_SECONDS, visibility_seconds: int = 60) -> QueuedMessage | None:
        response = self.client.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=max(0, min(MAX_LONG_POLL_SECONDS, int(wait_seconds))),
            VisibilityTimeout=max(0, int(visibility_seconds)),
            AttributeNames=["ApproximateReceiveCount"],
        )
        messages = response.get("Messages") or []
        if not messages:
            return None
        raw = messages[0]
        attempt = int((raw.get("Attributes") or {}).get("ApproximateReceiveCount", 1) or 1)
        try:
            job_id = json.loads(raw["Body"]).get("job_id")
        except (ValueError, TypeError, AttributeError):
            job_id = None
        message = QueuedMessage(raw["ReceiptHandle"], str(job_id or ""), attempt)
        if not job_id or not isinstance(job_id, str):
            # Not something a worker can act on. Keep it for inspection, not for retries.
            self.dead_letter(message, "message body carries no job id")
            return None
        return message

    def delete(self, message: QueuedMessage) -> None:
        self.client.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message.receipt)

    def retry_later(self, message: QueuedMessage, delay_seconds: int) -> None:
        self.client.change_message_visibility(
            QueueUrl=self.queue_url, ReceiptHandle=message.receipt, VisibilityTimeout=max(0, int(delay_seconds)),
        )

    def dead_letter(self, message: QueuedMessage, reason: str) -> None:
        if self.dead_letter_url:
            self.client.send_message(
                QueueUrl=self.dead_letter_url,
                MessageBody=json.dumps({"job_id": message.job_id, "reason": reason, "receives": message.attempt}),
            )
        self.delete(message)

    def healthy(self) -> bool:
        self.client.get_queue_attributes(QueueUrl=self.queue_url, AttributeNames=["QueueArn"])
        return True


def sqs_client(*, endpoint_url: str = "", region: str = "") -> Any:
    """A boto3 SQS client. Credentials come from the environment or the instance role."""
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - the sqs extra is optional
        raise RuntimeError("JOB_QUEUE_BACKEND=sqs needs boto3: pip install -e '.[sqs]'") from exc
    options: dict[str, Any] = {}
    if endpoint_url:
        options["endpoint_url"] = endpoint_url
    if region:
        options["region_name"] = region
    return boto3.client("sqs", **options)


def ensure_queues(client: Any, name: str, *, max_receives: int = DEFAULT_MAX_RECEIVES,
                  visibility_seconds: int = 300) -> tuple[str, str]:
    """Create (or find) the work queue and its dead-letter queue. Returns their URLs.

    The redrive policy hands a message to the dead-letter queue once it has
    been received `max_receives` times without being deleted. Idempotent:
    CreateQueue with the same name and attributes returns the existing queue.
    """
    dead = client.create_queue(QueueName=f"{name}-dead-letter", Attributes={"MessageRetentionPeriod": "1209600"})
    dead_url = dead["QueueUrl"]
    dead_arn = client.get_queue_attributes(QueueUrl=dead_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    main = client.create_queue(
        QueueName=name,
        Attributes={
            "VisibilityTimeout": str(int(visibility_seconds)),
            "ReceiveMessageWaitTimeSeconds": str(MAX_LONG_POLL_SECONDS),
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": dead_arn, "maxReceiveCount": int(max_receives)}),
        },
    )
    return main["QueueUrl"], dead_url


def main(argv: list[str] | None = None) -> int:
    """Create the work queue and its dead-letter queue, on AWS or LocalStack.

        python -m arranger_api.queue --create arranger-jobs
        python -m arranger_api.queue --create arranger-jobs --endpoint http://localhost:4566

    Prints the two URLs as the environment lines the API and worker need.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="python -m arranger_api.queue")
    parser.add_argument("--create", metavar="NAME", required=True, help="name of the work queue to create or find")
    parser.add_argument("--endpoint", default="", help="SQS-compatible endpoint, e.g. LocalStack; empty for AWS")
    parser.add_argument("--region", default="", help="AWS region; defaults to the environment's")
    parser.add_argument("--max-receives", type=int, default=DEFAULT_MAX_RECEIVES)
    parser.add_argument("--visibility-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    client = sqs_client(endpoint_url=args.endpoint, region=args.region)
    url, dead = ensure_queues(client, args.create, max_receives=args.max_receives,
                              visibility_seconds=args.visibility_seconds)
    print("JOB_QUEUE_BACKEND=sqs")
    print(f"SQS_QUEUE_URL={url}")
    print(f"SQS_DEAD_LETTER_QUEUE_URL={dead}")
    if args.endpoint:
        print(f"AWS_ENDPOINT_URL={args.endpoint}")
    return 0


def build_job_queue(settings: Any) -> JobQueue | None:
    """The queue the settings ask for, or None for the database-polling default."""
    backend = settings.job_queue_backend
    if backend == "database":
        return None
    if backend == "memory":
        return InMemoryQueue(max_receives=settings.job_queue_max_receives)
    if backend == "sqs":
        return SqsQueue(
            settings.sqs_queue_url,
            dead_letter_url=settings.sqs_dead_letter_queue_url,
            endpoint_url=settings.aws_endpoint_url,
            region=settings.aws_region,
            max_receives=settings.job_queue_max_receives,
        )
    raise ValueError(f"unknown JOB_QUEUE_BACKEND '{backend}'")


if __name__ == "__main__":  # pragma: no cover - operator tool
    raise SystemExit(main())
