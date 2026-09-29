"""The SQS adapter against a real (emulated) queue: LocalStack.

Set `ARRANGER_SQS_ENDPOINT` (for LocalStack, http://localhost:4566) and any
AWS credentials in the environment to run these; the CI job "SQS queue on
LocalStack" does. Skipped otherwise, with the reason.

    docker run --rm -p 4566:4566 -e SERVICES=sqs localstack/localstack:3
    ARRANGER_SQS_ENDPOINT=http://localhost:4566 AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test \
        AWS_DEFAULT_REGION=us-east-1 python -m pytest tests/test_sqs_localstack.py
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

ENDPOINT = os.environ.get("ARRANGER_SQS_ENDPOINT", "").strip()
REGION = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "us-east-1"

pytestmark = pytest.mark.skipif(
    not ENDPOINT, reason="ARRANGER_SQS_ENDPOINT is not set; the LocalStack queue tests run in the CI job"
)
boto3 = pytest.importorskip("boto3", reason="boto3 is not installed (pip install -e '.[sqs]')")

from conftest import build_settings, csrf_headers, make_client, register  # noqa: E402
from test_api_projects import PROFILE, upload  # noqa: E402

from arranger_api.queue import SqsQueue, ensure_queues  # noqa: E402
from arranger_api.worker import consume  # noqa: E402


@pytest.fixture
def queues():
    client = boto3.client("sqs", endpoint_url=ENDPOINT, region_name=REGION)
    name = f"arranger-test-{uuid.uuid4().hex[:10]}"
    url, dead = ensure_queues(client, name, max_receives=3, visibility_seconds=2)
    try:
        yield client, url, dead
    finally:
        client.delete_queue(QueueUrl=url)
        client.delete_queue(QueueUrl=dead)


def dead_letters(client, dead_url) -> list[str]:
    response = client.receive_message(QueueUrl=dead_url, MaxNumberOfMessages=10, WaitTimeSeconds=1)
    return [m["Body"] for m in response.get("Messages", [])]


def test_a_message_round_trips_and_stays_invisible_while_in_flight(queues):
    client, url, dead = queues
    queue = SqsQueue(url, dead_letter_url=dead, client=client)
    assert queue.healthy()
    assert queue.send("job-1")
    message = queue.receive(wait_seconds=2, visibility_seconds=10)
    assert message is not None and message.job_id == "job-1" and message.attempt == 1
    assert queue.receive(wait_seconds=1, visibility_seconds=10) is None, "invisible to a second worker"
    queue.delete(message)
    assert queue.receive(wait_seconds=1, visibility_seconds=10) is None
    assert dead_letters(client, dead) == []


def test_the_redrive_policy_moves_a_message_after_three_receives(queues):
    client, url, dead = queues
    queue = SqsQueue(url, dead_letter_url=dead, client=client)
    queue.send("job-2")
    for attempt in (1, 2, 3):
        message = queue.receive(wait_seconds=2, visibility_seconds=0)   # never acknowledged: the worker died
        assert message is not None and message.attempt == attempt, f"delivery {attempt}"
    assert queue.receive(wait_seconds=2, visibility_seconds=0) is None, "the fourth receive finds it gone"
    assert any('"job-2"' in body for body in dead_letters(client, dead))


def test_a_job_moves_through_the_real_queue_end_to_end(queues, tmp_path):
    client, url, dead = queues
    settings = build_settings(
        sqlite_path=str(tmp_path / "sqs.db"), artifact_dir=str(tmp_path / "files"), job_workers=0,
        job_queue_backend="sqs", sqs_queue_url=url, sqs_dead_letter_queue_url=dead,
        aws_endpoint_url=ENDPOINT, aws_region=REGION, quota_active_jobs=5,
    )
    with make_client(settings=settings) as api:
        assert api.get("/ready").json()["queue"] == "sqs"
        register(api)
        project_id = upload(api).json()["project"]["id"]
        response = api.post("/jobs/arrange", json={"project_id": project_id, "profile": PROFILE},
                            headers=csrf_headers(api))
        assert response.status_code == 202, response.text
        job = response.json()["job"]
        assert job["status"] == "queued"

        counts = consume(api.app.state.job_runner, api.app.state.job_queue, once=True, wait_seconds=2)
        assert counts["ran"] == 1, counts
        final = api.get(f"/jobs/{job['id']}").json()["job"]
        assert final["status"] == "succeeded" and final["result"]["arrangement_id"]
        assert api.app.state.job_queue.receive(wait_seconds=1, visibility_seconds=5) is None, "acknowledged"
        assert dead_letters(client, dead) == []
