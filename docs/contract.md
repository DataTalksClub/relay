# Relay status contract

Version 1. Draft.

Every project exposes one read-only endpoint. The console polls it. Nothing
else consumes it, and nothing writes to it.

## Endpoint

```
GET /internal/ops/status
Authorization: Bearer <per-project token>
```

A bearer token is sufficient. This is a read-only server-to-server GET over
TLS; the replay protection a signed request would add buys nothing here.

Responses are cached server side for 30 seconds, so polling costs one set of
indexed queries per half minute regardless of how many people have the console
open.

## Payload

```json
{
  "contract_version": 1,
  "project": "cmp",
  "version": "<deployed app tag>",
  "generated_at": "2026-08-06T09:14:22Z",

  "worker": {
    "mode": "sidecar",
    "last_seen": "2026-08-06T09:14:19Z",
    "healthy": true
  },

  "queue": {
    "pending": 3,
    "oldest_pending_age_s": 12
  },

  "schedules": [
    {
      "name": "deadline-reminders",
      "cron": "0 9 * * *",
      "last_run": "2026-08-06T09:00:04Z",
      "last_success": "2026-08-06T09:00:11Z",
      "next_run": "2026-08-07T09:00:00Z",
      "enabled": true
    }
  ],

  "recent_runs": [
    {
      "id": "0198f2...",
      "correlation_id": "0198f2...",
      "name": "send-campaign",
      "status": "running",
      "started": "2026-08-06T09:12:00Z",
      "duration_s": 142,
      "baseline_p50_s": 38,
      "progress": { "current": 1400, "total": 5000 },
      "message": "batch 7 of 25",
      "entity": { "label": "August newsletter", "url": "/studio/campaigns/42/" }
    }
  ],

  "failures_24h": 2
}
```

## Field notes

`worker.healthy` is derived from heartbeat age, not from asking the platform.
A worker that is running but wedged must read as unhealthy, which a
platform-level liveness check will not catch.

`queue.oldest_pending_age_s` is the single most useful number on the page. It
is the one that distinguishes "quiet" from "stopped", and it is what the paging
alarm should watch.

`progress` is null for workloads with no meaningful numerator. Consumers must
render duration-versus-baseline in that case rather than a zero-length bar.

`baseline_p50_s` is a rolling median of prior successful runs of the same task
name. It exists so that "running for 142s" becomes "running for 142s, normally
38s", which is the difference between a number and a signal.

`entity` is resolved per project against that project's own domain models. It
is optional and always nullable — the console must render a run with no entity.

`correlation_id` equals `id` for work that started locally, and equals the
originator's identifier for work triggered from another service. This is what
makes a cross-service chain display as one thing.

## Versioning

`contract_version` is required in every response.

The console must accept any version it knows, and degrade rather than fail on
an unknown one: render what it recognises, mark the rest unavailable. Projects
deploy independently and will be on different versions in normal operation, not
just during a rollout.

Additive changes do not bump the version. Removing or repurposing a field does.

## What is deliberately absent

No task submission. The contract is read-only; there is no path from the
console to enqueueing or cancelling work. A console that can only observe
cannot cause an outage.

No engagement metrics. Opens, clicks, and other outcomes that arrive long after
a task ends belong to the domain model, not the task system. Including them
here would make tasks appear to run for days.

No log content. The contract carries a short `message` and an `error`, not
output. Logs stay in the platform's log store.
