# Asynchronous jobs

Arranging, engraving and transcribing run as background jobs. A job is a row
in the `jobs` table: status (`queued`, `running`, `succeeded`, `failed`,
`cancelled`), progress, attempts, result and error. The browser starts one and
polls it:

```text
POST /jobs/arrange      -> 202 {"job": {"id": ..., "status": "queued"}}
GET  /jobs/{id}         -> {"job": {"status": "running", "progress": 0.4, ...}}
GET  /jobs/{id}         -> {"job": {"status": "succeeded", "result": {"arrangement_id": ...}}}
```

`POST /projects/{id}/arrangements` does the same with the project id in the
path. Both honour an `Idempotency-Key` header: a retried request gets the job
it already started, and no second message.

## Who runs the job

`JOB_QUEUE_BACKEND` chooses how a worker learns that a job exists:

| Backend | How workers find work | Use it when |
|---|---|---|
| `database` (default) | Workers poll the `jobs` table and claim rows under a lease | One host, or a few sharing one database |
| `sqs` | The API sends one message per job to Amazon SQS; workers long-poll the queue | Several worker hosts, or workers that scale independently of the API |
| `memory` | An in-process queue with SQS semantics | Tests and development, never production (the app refuses it) |

The table stays the record of truth in every mode. The queue only carries
"job `<id>` is ready"; a worker that receives the message claims the row
with a conditional update (`queued` -> `running`) and runs it.

### Delivery guarantees

SQS delivers at least once, so everything a worker does is idempotent by job
id (`JobRunner.run_job`):

- A message whose job is no longer `queued` (already claimed by another
  delivery, finished, or cancelled) is dropped and acknowledged. A message
  delivered twice runs the job once; `tests/test_job_queue.py` proves it.
- The message is deleted only after the job row reached a final state. If the
  worker dies mid-job the message stays invisible for the visibility timeout
  (`JOB_VISIBILITY_SECONDS`, by default the slowest job's limit plus a minute)
  and is then delivered again; the row's lease expires on the same clock and
  housekeeping requeues it.
- A job that asks for another attempt (`JobFailed(retryable=True)` or an
  unexpected error under `JOB_MAX_ATTEMPTS`) is backed off through the
  message's visibility, not re-sent.
- After `JOB_QUEUE_MAX_RECEIVES` (3) failed deliveries the message goes to the
  dead-letter queue and the row is failed with `error.code = dead_lettered`.
  The queue's redrive policy does the same for messages that were received
  three times without ever being acknowledged.

## Setting up SQS

Needs the `sqs` extra (`pip install -e ".[sqs]"`, which installs boto3) and
AWS credentials in the environment or an instance role.

Create the work queue and its dead-letter queue in one go; the command prints
the environment lines to set:

```bash
python -m arranger_api.queue --create arranger-jobs
```

```text
JOB_QUEUE_BACKEND=sqs
SQS_QUEUE_URL=https://sqs.eu-west-1.amazonaws.com/123456789012/arranger-jobs
SQS_DEAD_LETTER_QUEUE_URL=https://sqs.eu-west-1.amazonaws.com/123456789012/arranger-jobs-dead-letter
```

The queue is created with a 20-second receive wait, a visibility timeout of
300 seconds and a redrive policy of three receives. Rerunning the command is
safe: it finds the existing queues.

LocalStack works the same way, with an endpoint:

```bash
docker run --rm -p 4566:4566 -e SERVICES=sqs localstack/localstack:3
AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1 \
  python -m arranger_api.queue --create arranger-jobs --endpoint http://localhost:4566
```

CI runs `tests/test_sqs_localstack.py` against a LocalStack service container:
a round trip, the redrive to the dead-letter queue after three receives, and a
job that goes queued -> running -> succeeded through the real queue.

## Running the worker

The API sends messages; workers consume them:

```bash
JOB_QUEUE_BACKEND=sqs SQS_QUEUE_URL=... SQS_DEAD_LETTER_QUEUE_URL=... arranger-worker --workers 2
arranger-worker --once      # drain what is waiting and exit
```

Each worker thread long-polls with `WaitTimeSeconds=20` and receives one
message at a time. Set `JOB_WORKERS=0` on the API service so it stops polling
the table itself; the API only dispatches.

### As a second Railway service

Railway runs one command per service from the same image, so the worker is a
second service on the same repository:

1. In the Railway project, add a new service from the same GitHub repository
   (same Dockerfile). Set its start command to `arranger-worker --workers 2`.
2. Give both services the same variables (`DATABASE_URL`, `ARTIFACT_BACKEND`
   and friends, `JOB_QUEUE_BACKEND=sqs`, the two `SQS_*` URLs, `AWS_REGION`,
   `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` for a user whose policy
   allows `sqs:SendMessage`, `sqs:ReceiveMessage`, `sqs:DeleteMessage`,
   `sqs:ChangeMessageVisibility` and `sqs:GetQueueAttributes` on the two
   queues).
3. Set `JOB_WORKERS=0` on the web service.
4. Deploy the worker first, then the web service; jobs queued in between wait
   in SQS.

`/ready` on the API reports `"queue": "sqs"` and answers 503
`job_queue_unavailable` when the queue cannot be reached. Watch the
dead-letter queue's `ApproximateNumberOfMessagesVisible` in CloudWatch: it
should stay at zero.

Creating the queues, the IAM user and the Railway service are account
changes; the steps above are the whole of what they need.
