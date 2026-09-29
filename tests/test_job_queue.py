"""The job queue port: SQS semantics in memory, the SQS adapter against a fake
client, and the worker loop driven through the API.

Delivery is at least once, so the tests that matter are the ones about
duplicates and failure: a message delivered twice runs the job once, a job
moves queued -> running -> succeeded end to end, and a message that keeps
failing goes to the dead-letter queue after three deliveries.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import build_settings, csrf_headers, make_client, register  # noqa: E402
from test_api_projects import PROFILE, upload  # noqa: E402

from arranger_api.jobs import JobFailed  # noqa: E402
from arranger_api.queue import InMemoryQueue, QueuedMessage, SqsQueue, build_job_queue, ensure_queues  # noqa: E402
from arranger_api.settings import configuration_problems  # noqa: E402
from arranger_api.worker import consume  # noqa: E402


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


# --- the in-memory queue has SQS semantics --------------------------------------


def test_memory_queue_delivers_once_until_the_visibility_timeout():
    clock = Clock()
    queue = InMemoryQueue(clock=clock)
    queue.send("job-1")
    first = queue.receive(wait_seconds=0, visibility_seconds=30)
    assert first is not None and first.job_id == "job-1" and first.attempt == 1
    assert queue.receive(wait_seconds=0, visibility_seconds=30) is None, "in flight: invisible to a second worker"
    clock.now += 31
    again = queue.receive(wait_seconds=0, visibility_seconds=30)
    assert again is not None and again.job_id == "job-1" and again.attempt == 2
    queue.delete(again)
    clock.now += 100
    assert queue.receive(wait_seconds=0, visibility_seconds=30) is None
    assert queue.in_flight == 0 and queue.pending == 0


def test_memory_queue_retry_later_shortens_the_wait():
    clock = Clock()
    queue = InMemoryQueue(clock=clock)
    queue.send("job-1")
    message = queue.receive(wait_seconds=0, visibility_seconds=300)
    queue.retry_later(message, 5)
    clock.now += 6
    again = queue.receive(wait_seconds=0, visibility_seconds=300)
    assert again is not None and again.attempt == 2


def test_memory_queue_dead_letters_after_max_receives():
    clock = Clock()
    queue = InMemoryQueue(max_receives=3, clock=clock)
    queue.send("job-1")
    for attempt in (1, 2, 3):
        message = queue.receive(wait_seconds=0, visibility_seconds=1)
        assert message is not None and message.attempt == attempt
        clock.now += 2   # the worker died: the message comes back
    assert queue.receive(wait_seconds=0, visibility_seconds=1) is None
    assert [d["job_id"] for d in queue.dead_letters] == ["job-1"]
    assert queue.dead_letters[0]["reason"] == "too many deliveries"


def test_memory_queue_explicit_dead_letter_and_an_empty_wait():
    queue = InMemoryQueue()
    assert queue.receive(wait_seconds=0, visibility_seconds=1) is None
    queue.send("job-2")
    message = queue.receive(wait_seconds=0, visibility_seconds=60)
    queue.dead_letter(message, "poison")
    assert queue.dead_letters[0]["reason"] == "poison" and queue.in_flight == 0


# --- the SQS adapter, against a fake client ---------------------------------------


class FakeSqs:
    """Just enough of boto3's SQS client to see what the adapter asks of it."""

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self.visibility: list[tuple[str, int]] = []
        self.inbox: list[list[dict]] = []
        self.queues: dict[str, dict] = {}
        self.last_receive: dict = {}

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append((QueueUrl, json.loads(MessageBody)))
        return {"MessageId": f"m{len(self.sent)}"}

    def receive_message(self, **kwargs):
        self.last_receive = kwargs
        return {"Messages": self.inbox.pop(0)} if self.inbox else {}

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)

    def change_message_visibility(self, QueueUrl, ReceiptHandle, VisibilityTimeout):
        self.visibility.append((ReceiptHandle, VisibilityTimeout))

    def get_queue_attributes(self, QueueUrl, AttributeNames):
        return {"Attributes": {"QueueArn": f"arn:{QueueUrl}"}}

    def create_queue(self, QueueName, Attributes):
        self.queues[QueueName] = Attributes
        return {"QueueUrl": f"http://sqs/{QueueName}"}


def test_sqs_adapter_sends_receives_backs_off_and_deletes_by_receipt():
    client = FakeSqs()
    queue = SqsQueue("http://sqs/work", dead_letter_url="http://sqs/dead", client=client)
    assert queue.send("job-9") == "m1"
    assert client.sent == [("http://sqs/work", {"job_id": "job-9"})]

    client.inbox.append([{"ReceiptHandle": "r1", "Body": json.dumps({"job_id": "job-9"}),
                          "Attributes": {"ApproximateReceiveCount": "2"}}])
    message = queue.receive(wait_seconds=20, visibility_seconds=240)
    assert message == QueuedMessage("r1", "job-9", 2)
    assert client.last_receive["WaitTimeSeconds"] == 20
    assert client.last_receive["VisibilityTimeout"] == 240
    assert client.last_receive["MaxNumberOfMessages"] == 1
    queue.retry_later(message, 8)
    assert client.visibility == [("r1", 8)]
    queue.delete(message)
    assert client.deleted == ["r1"]
    assert queue.receive(wait_seconds=0, visibility_seconds=1) is None
    assert queue.healthy()


def test_sqs_adapter_dead_letters_explicitly_and_drops_a_poison_message():
    client = FakeSqs()
    queue = SqsQueue("http://sqs/work", dead_letter_url="http://sqs/dead", client=client)
    queue.dead_letter(QueuedMessage("r7", "job-7", 3), "gave up")
    assert client.sent == [("http://sqs/dead", {"job_id": "job-7", "reason": "gave up", "receives": 3})]
    assert client.deleted == ["r7"]

    client.inbox.append([{"ReceiptHandle": "r8", "Body": "not json at all", "Attributes": {}}])
    assert queue.receive(wait_seconds=0, visibility_seconds=1) is None
    assert client.deleted[-1] == "r8", "a body nobody can act on is removed, not retried forever"
    assert client.sent[-1][0] == "http://sqs/dead"


def test_ensure_queues_creates_the_pair_with_a_redrive_policy():
    client = FakeSqs()
    url, dead = ensure_queues(client, "arranger-jobs", max_receives=3, visibility_seconds=240)
    assert url == "http://sqs/arranger-jobs" and dead == "http://sqs/arranger-jobs-dead-letter"
    policy = json.loads(client.queues["arranger-jobs"]["RedrivePolicy"])
    assert policy == {"deadLetterTargetArn": "arn:http://sqs/arranger-jobs-dead-letter", "maxReceiveCount": 3}
    assert client.queues["arranger-jobs"]["VisibilityTimeout"] == "240"
    assert client.queues["arranger-jobs"]["ReceiveMessageWaitTimeSeconds"] == "20"


def test_build_job_queue_follows_the_setting_and_validation_catches_a_missing_url():
    assert build_job_queue(build_settings(job_queue_backend="database")) is None
    assert isinstance(build_job_queue(build_settings(job_queue_backend="memory")), InMemoryQueue)
    with pytest.raises(ValueError):
        build_job_queue(build_settings(job_queue_backend="carrier-pigeon"))
    problems = configuration_problems(build_settings(job_queue_backend="sqs"))
    assert any("SQS_QUEUE_URL" in p for p in problems)
    assert build_settings(arrange_max_seconds=180, transcribe_max_seconds=900).effective_visibility_seconds == 960
    assert build_settings(job_visibility_seconds=45).effective_visibility_seconds == 45


# --- the worker loop over the API ---------------------------------------------------


@pytest.fixture
def api(tmp_path):
    settings = build_settings(
        sqlite_path=str(tmp_path / "queue.db"), artifact_dir=str(tmp_path / "files"),
        job_workers=0, job_queue_backend="memory", job_max_attempts=10, quota_active_jobs=5,
    )
    with make_client(settings=settings) as client:
        register(client)
        client.project_id = upload(client).json()["project"]["id"]
        yield client


def arrange_job(api) -> dict:
    response = api.post("/jobs/arrange", json={"project_id": api.project_id, "profile": PROFILE},
                        headers=csrf_headers(api))
    assert response.status_code == 202, response.text
    return response.json()["job"]


def test_arrange_job_is_accepted_dispatched_and_runs_queued_running_succeeded(api):
    queue = api.app.state.job_queue
    assert isinstance(queue, InMemoryQueue)
    assert api.get("/ready").json()["queue"] == "memory"

    job = arrange_job(api)
    assert job["status"] == "queued"
    assert queue.sent == [job["id"]], "the message names the row that was just committed"
    assert api.get(f"/jobs/{job['id']}").json()["job"]["status"] == "queued"

    seen: list[str] = []
    runner = api.app.state.job_runner
    real = runner.handlers["arrange"]

    def observing(ctx):
        seen.append(api.get(f"/jobs/{ctx.job['id']}").json()["job"]["status"])
        return real(ctx)

    runner.handlers = {**runner.handlers, "arrange": observing}
    counts = consume(runner, queue, once=True, wait_seconds=0)
    assert counts == {"ran": 1, "skipped": 0, "retried": 0, "dead_lettered": 0}
    assert seen == ["running"]
    final = api.get(f"/jobs/{job['id']}").json()["job"]
    assert final["status"] == "succeeded" and final["result"]["arrangement_id"]
    assert queue.in_flight == 0 and queue.pending == 0, "deleted only after the row reached its final state"


def test_a_message_delivered_twice_runs_the_job_once(api):
    queue = api.app.state.job_queue
    job = arrange_job(api)
    queue.send(job["id"])   # the same job announced again, as a redelivery after a lost acknowledgement would
    runs: list[str] = []
    runner = api.app.state.job_runner
    real = runner.handlers["arrange"]

    def counting(ctx):
        runs.append(ctx.job["id"])
        return real(ctx)

    runner.handlers = {**runner.handlers, "arrange": counting}
    counts = consume(runner, queue, once=True, wait_seconds=0)
    assert counts["ran"] == 1 and counts["skipped"] == 1
    assert runs == [job["id"]]
    assert len(api.get(f"/projects/{api.project_id}").json()["arrangements"]) == 1
    assert queue.pending == 0 and queue.in_flight == 0


def test_a_job_that_keeps_failing_is_dead_lettered_after_three_deliveries(api):
    queue: InMemoryQueue = api.app.state.job_queue
    job = arrange_job(api)
    runner = api.app.state.job_runner

    def flaky(ctx):
        raise JobFailed("flaky", "The arranging service is busy; try again.", retryable=True)

    runner.handlers = {**runner.handlers, "arrange": flaky}
    for delivery in (1, 2, 3):
        counts = consume(runner, queue, once=True, wait_seconds=0)
        if delivery < 3:
            assert counts["retried"] == 1 and counts["dead_lettered"] == 0
            assert api.get(f"/jobs/{job['id']}").json()["job"]["status"] == "queued"
            queue.expire_in_flight()   # the back-off elapses
        else:
            assert counts["dead_lettered"] == 1
    assert [d["job_id"] for d in queue.dead_letters] == [job["id"]]
    final = api.get(f"/jobs/{job['id']}").json()["job"]
    assert final["status"] == "failed" and final["error"]["code"] == "dead_lettered"
    assert queue.in_flight == 0 and queue.pending == 0


def test_the_project_route_dispatches_too_and_a_repeat_with_the_same_key_does_not(api):
    queue = api.app.state.job_queue
    headers = {**csrf_headers(api), "Idempotency-Key": "same-request-twice"}
    first = api.post(f"/projects/{api.project_id}/arrangements", json={"profile": PROFILE}, headers=headers)
    second = api.post(f"/projects/{api.project_id}/arrangements", json={"profile": PROFILE}, headers=headers)
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["job"]["id"] == second.json()["job"]["id"]
    assert queue.sent == [first.json()["job"]["id"]], "one job, one message"


def test_arrange_job_needs_the_users_own_project(api):
    response = api.post("/jobs/arrange", json={"project_id": "00000000-0000-4000-8000-000000000000", "profile": PROFILE},
                        headers=csrf_headers(api))
    assert response.status_code == 404
    assert api.app.state.job_queue.sent == []
