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

## Acceptance gaps and API errors

`run_relay_scheduler` also checks transactional acceptance every 15 seconds. A
client with at least 70 acceptances in the trailing seven days (10/day) pages
when its latest acceptance is at least 60 minutes old. All hours are active by
default: CMP serves a global audience. Clients with no traffic history do not
page. An explicitly configured quiet hour suppresses the gap check until a
full active hour has elapsed. With a healthy scheduler, detection is at the
60-minute threshold plus at most one 15-second scheduler tick.

`RELAY_TRANSACTIONAL_MONITOR_CLIENTS` is JSON keyed by numeric client ID, e.g.
`{"2":{"min_daily":10,"timezone":"Europe/Berlin","quiet_hours":[0,1,2,3,4,5],"error_percent":20,"min_requests":5,"min_requests_4xx":20}}`.
Use `enabled:false` for a client to disable its checks. Relay-side HTTP 5xx page
at >=20% over the last 15 minutes with at least 5 requests, including at night,
so a real server outage is never muted. Client-side HTTP 4xx (bad input, bad or
revoked key, operator auth probes) contribute to a combined 4xx/5xx check:
>=20% errors with at least 20 requests in 15 minutes (`min_requests_4xx`).
Mixed failures therefore count even when neither class alone reaches 20%.
A single 4xx probe at low traffic does not
page (2026-10-24: one 401 probe among 5 requests for client 2 paged before this
split and would stay silent now). A total 4xx stoppage with zero acceptances is
still covered by the acceptance-gap check. Minute counters expire after a day.
The boundary minute is excluded, giving up to one minute of rounding at the
start of that window.

The middleware stores only client ID, minute, total, error and server-error (5xx)
counts. It uses
the authenticated client or, on authentication failure, the key's public ID as
an attribution hint (including revoked keys). An unknown/missing key cannot be
assigned to a client. Public-ID attribution is not proof of caller identity.
Responses rejected by a CDN/WAF/load balancer never reach this middleware;
those require edge telemetry. A total stoppage is still covered by the gap
check. Database outages can also prevent counters and the scheduler from
working; existing external ops availability checks remain necessary.

Production alerts use the same msmtp/SES SMTP path as the host's ops alerts,
with From `noreply@datatalks.club` and recipient `dev@dtcdev.click`. The scheduler
alone mounts `/etc/msmtprc` read-only at `/run/relay-ops-msmtprc`; credentials
stay in the existing host file and are never copied into git or environment
variables. The image includes msmtp. `RELAY_TRANSACTIONAL_MONITOR_MSMTP_CONFIG`
selects this config file; without it the monitor uses Relay's SES API transport.
The production send role cannot send the ops identity, so the SMTP mount is
required there. Override From/To with `RELAY_TRANSACTIONAL_MONITOR_FROM` / `_TO`.
Delivery is synchronous and does not depend on the transactional worker queue.
Enforced Relay dry-run blocks both transports. SMTP has a bounded 20-second
process timeout, and its diagnostics are suppressed to protect credentials.
Successful pages repeat at most once per hour per client/reason, persisted in
the database across scheduler restarts; failed sends remain eligible for retry.
The scheduler emits `relay_transactional_monitor_heartbeat` each check and
`relay_transactional_alert_delivery_failed` if delivery fails, without error
text or private mail data. `RELAY_TRANSACTIONAL_MONITOR_ENABLED=0` disables it.

Run `uv run python manage.py check_transactional_health` for an immediate check.
`--test-alert` sends a clearly marked forced test, hard-coded to
`dev@dtcdev.click` regardless of configured recipients. Neither command resends
client messages or changes their delivery state.

## SES delivery gaps and webhook ingestion monitoring

Relay guards against unconfirmed SES deliveries and dropped event notifications:

1. **SES Outcome Gap**: Sent messages older than 2 hours that have neither a delivery,
   bounce, nor complaint event are flagged. If the count reaches
   `RELAY_SES_OUTCOME_GAP_THRESHOLD` (default: 1), an alert is triggered.
   Transient delivery delays (such as SES retrying a null-MX host) keep messages in
   `sent` state until SES either succeeds or hard-bounces after its 14-hour retry window.
   An unexpired `ses.delivery_delay` audit suppresses the gap for that send until
   its `expiration_time` (fallback: 14h from the audit); after expiry with still no
   terminal event, the gap pages again.
2. **SNS ingress vs. Relay Persisted Gap**: Over a rolling 30-minute window ending
   two minutes ago (minute-aligned), exact SNS IDs from durable
   `mailing.tasks.process_ses_webhook_event` ingress receipts are checked against
   `EmailEvent` and SES `OperatorAudit` IDs. SQS is acknowledged only after the
   receipt commits. Both sides use the same `metadata.sns_timestamp` cohort.
   Late database creation and duplicate deliveries do not create deficits;
   unrelated persisted IDs cannot hide a missing notification.
   CloudWatch SNS publication counts are diagnostic context: their minute
   buckets can differ from the SNS envelope clock even by milliseconds and
   must not independently page on a cross-clock count difference.
   A first sighting only starts a pending
   window (`relay_ses_gap_pending`); the page fires only if the gap is still
   present after `RELAY_SNS_GAP_CONFIRM_MINUTES` (default: 5). Confirmation
   rechecks the original fixed publication window. Late persistence resolves
   that cohort; later publications cannot mask loss, and loss cannot age out
   during confirmation. Failed CloudWatch reads preserve the pending cohort.
   Pending state is process-local, so a restart starts a fresh confirmation.
   Task-result retention must exceed the monitoring/confirmation interval.
   `force=True` (manual
   `check_ses_delivery_gap --force`) bypasses the confirmation for verification.
3. **Transport before ingress**: For the configured SES SQS queue, SNS
   `NumberOfNotificationsFailed` pages terminal delivery failures, nonempty
   DLQ depth pages poison/unpersisted messages, and a nonempty source queue with
   oldest-message age of at least five minutes pages stalled ingestion. These
   checks do not rely on a provider task existing. Transport query failures are
   logged explicitly. All SES/SNS pages include the emitting container, image
   tag, and source SHA.

Both alerts dispatch through the existing ops alert transport (`send_monitor_alert` /
msmtp SMTP / SES fallback) with repeat throttling (1 hour per kind).
The scheduler executes `check_ses_delivery_gap()` on every tick.
Immediate checks and forced tests can be run via:
`uv run python manage.py check_ses_delivery_gap [--force] [--test-alert]`.

### SQS Consumer Ingress Safety

SQS consumers (`SqsWorker` and `process_sqs_event`) ensure that any failure during
ingress processing (handler exception, non-dict response, or unidentifiable record failure)
retains the message in SQS. Unacknowledged failed messages are retried up to `maxReceiveCount`
and routed directly to the SQS Dead Letter Queue (DLQ) for operator inspection, preventing
accidental acknowledgment or silent deletion of unpersisted messages.
