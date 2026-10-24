# Transactional send reliability

Relay's database task backend records failed attempts but has no automatic retry
policy. `mailing.tasks.send_transactional_email` queues a delayed replacement
when its sender raises, then lets the original task record the failure.
Retry scheduling requires the task database to be writable. A database outage
that also prevents enqueueing leaves the original task failed; queued-age
monitoring raises the backlog alert once the database is readable again.

Environment configuration:

| Setting | Default | Meaning |
| --- | --- | --- |
| `RELAY_TRANSACTIONAL_MAX_ATTEMPTS` | `5` | Total attempts, including the first send |
| `RELAY_TRANSACTIONAL_RETRY_BASE_SECONDS` | `60` | First retry delay; doubles after each failure |
| `RELAY_TRANSACTIONAL_RETRY_MAX_SECONDS` | `900` | Maximum retry delay |
| `RELAY_TRANSACTIONAL_QUEUED_ALERT_MINUTES` | `15` | Alert when any queued message exceeds this age |
| `RELAY_TRANSACTIONAL_ALERT_REPEAT_SECONDS` | `300` | Repeat alerts while the backlog persists |
| `RELAY_TRANSACTIONAL_ALERT_WEBHOOK_URL` | empty | Optional HTTP endpoint receiving the alert JSON |

The default retry delays are 60, 120, 240 and 480 seconds. Exhaustion sets the
message to `failed` and records a `failed` event with reason `retry_exhausted`.
Permanent SES failures still fail immediately. Already terminal messages do not
send again. A failure after SES acceptance can leave a `sending` claim; retries
never release that claim or call SES again. On exhaustion this is recorded as
`failed` with `delivery_uncertain=true`. Reconcile SES evidence before any manual
release of such a message.

`run_relay_scheduler` checks queued age every pass (normally 15 seconds), using
creation time so error updates cannot hide a stuck message. It emits an ERROR
log event `relay_transactional_queued_too_long` and optionally POSTs the same
JSON to the configured webhook. On Contabo, Vector collects scheduler logs in
central logging. Use that ERROR event for log alerts, or configure an HTTP
receiver for paging. The payload includes
count, threshold, oldest age, and at most 50 numeric message IDs. It contains no
recipient addresses, message bodies, idempotency keys or credentials. Webhook
errors emit a separate ERROR containing only the exception type. Repeats are
limited per scheduler process; restarting the scheduler allows an immediate
alert. This monitor never resends historical messages.

Retry tasks must be persisted in the database. A database outage that prevents
that insert leaves the original task failed; once database access returns,
the queued-age monitor reports the unresolved message for operator recovery.
