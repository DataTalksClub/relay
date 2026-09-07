# Relay

Relay runs email and background jobs for DataTalksClub services. A client calls
the HTTP API, receives a task ID and reads status from the same service. Client
projects don't install Relay or deploy their own worker.

The sandbox runs at [relay.dtcdev.click](https://relay.dtcdev.click) on a small
ARM EC2 instance in `eu-west-1`. We run the web server, task worker, scheduler,
AWS ingress drains and Postgres as separate containers on that host. Caddy
terminates HTTPS.

## Submit a task

Every submission needs a client API key and an idempotency key. Repeating the
same request returns the original task. Reusing the key for different work
returns `409 Conflict`.

```bash
export RELAY_URL=https://relay.dtcdev.click
export RELAY_API_KEY='<client-api-key>'

curl -sS -X POST "$RELAY_URL/api/tasks" \
  -H "Authorization: Bearer $RELAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "system.echo",
    "idempotency_key": "example-001",
    "params": {"message": "hello"}
  }'
```

Read the returned task until it reaches `succeeded` or `failed`:

```bash
curl -sS "$RELAY_URL/api/tasks/<task-id>" \
  -H "Authorization: Bearer $RELAY_API_KEY"
```

Relay currently runs three task types:

- `system.echo` proves the API, database and worker path without side effects.
- `email.send` accepts the same `params` as `/api/transactional/send` and queues
  the message in Relay's durable email queue.
- `webhook` sends a signed HTTPS request to a registered client origin. Relay
  retries timeouts, `429` responses and server errors with exponential backoff,
  but it doesn't retry ordinary `4xx` responses.

Relay adds `X-Relay-Task-Id`, `X-Relay-Correlation-Id`, `X-Relay-Timestamp`,
`X-Relay-Attempt` and `X-Relay-Signature` to each webhook request. It computes
the signature as HMAC-SHA256 over `<timestamp>.<raw-json-body>` with the
client's webhook secret, and a webhook may run for at most 60 seconds.

A receiver with longer work can answer `202` with `{"lease_seconds": N}` and
complete or fail the task later through `POST /api/tasks/{id}/complete` or
`/fail`. Relay keeps the task `running` under a lease and fails it if the lease
expires without a callback. The retry table, per-client limits and the lease
protocol are documented in [docs/api.md](docs/api.md), together with a
reference receiver that rejects replayed timestamps.

## Create a schedule

Relay stores cron schedules next to task status. The scheduler records the last
run, last success, next run and the last missed time. After downtime it fires
once and records the missed time instead of sending an unbounded catch-up
burst.

```bash
curl -sS -X POST "$RELAY_URL/api/schedules" \
  -H "Authorization: Bearer $RELAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "daily-refresh",
    "cron": "0 9 * * *",
    "type": "webhook",
    "url": "https://courses.example.com/internal/jobs/refresh",
    "params": {"scope": "daily"}
  }'
```

Register the webhook origin and signing secret on the Relay client before you
create a webhook task or schedule. Relay rejects every other origin.

## Run locally

Install dependencies with `uv`, then start Postgres and Django:

```bash
uv sync
docker compose up --build web worker scheduler
```

The app listens at `http://localhost:8001` after the local container migrates
the database and seeds demo clients, templates and API keys.

Run checks without Docker:

```bash
uv run ruff check .
uv run python manage.py makemigrations --check --dry-run
uv run python manage.py check
uv run pytest
```

## Deploy the sandbox

Pushing `main` runs the test suite and deploys the complete release through
AWS Systems Manager. The host runs
[`scripts/deploy_relay_sandbox.sh`](scripts/deploy_relay_sandbox.sh), then fails
the deployment unless every required container is running and the worker
finishes a queued `system.echo` task.

We store Postgres and Caddy data on the encrypted EBS volume mounted at
`/var/lib/relay`. Terraform owns the host, DNS, SES identities, AWS ingress
queues and IAM roles in `DataTalksClub/aws-infra/sandbox/relay`.

Relay uses a task-scoped IAM role for `email.send`. The web and worker instance
role can assume it but can't call SES directly. If a deployed task type lacks
a role mapping, Relay fails the task rather than falling back to the instance
role.

See [docs/relay-deployment.md](docs/relay-deployment.md) for deployment checks,
logs and rollback commands.

The production deploy path reuses the same script with
`--environment production` and is described in
[docs/production.md](docs/production.md). Production is not deployed yet.

## Migration safety

Relay uses separate infrastructure and a separate database from Datamailer.
Deploying Relay doesn't change or restart Datamailer. Move each client only
after Relay has passed its own email and webhook smoke checks.

Read [docs/context.md](docs/context.md) for the estate history and
[docs/requirements.md](docs/requirements.md) for the design constraints.
