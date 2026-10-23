# Relay auxiliary ownership: inbound mail and pruning (production, Contabo)

Date: 2026-10-23. Scope: `relay-staging` on Contabo `.141` (Coolify service
`fjnzwomg8n42lztloo34just`, image pinned by CD). Sibling service `relay-dev`
(`t9uudl5ik40webxrzamrm8qy`) is out of scope and untouched.

This is the explicit owner record for the two auxiliary paths the t1143u DR
audit (O10) found ownerless: production inbound mail and pruning. One of them
is intentionally disabled; the other now has a single recurring owner in code.

## 1. Production inbound mail: intentionally disabled, fail-closed

There is deliberately **no inbound-mail consumer in production**:

- `inbound_addresses`: **0 rows**; `inbound_messages`: **0 rows** (live SQL,
  2026-10-23). No address was ever provisioned in production.
- The relay environment has **no `SQS_INBOUND_EMAIL_QUEUE_URL`** (nor sandbox
  variants). `mailing/ingress.py::get_ingress_config("inbound-email")` raises
  `ImproperlyConfigured` without it, so a drain worker **cannot start by
  accident** — demonstrated live 2026-10-23 (traceback on
  `drain_sqs_ingress inbound-email --once`, no AWS call made).
- The deployed service set (`deploy/coolify/docker-compose.staging.yml`)
  contains eight containers and no inbound drain: production was cut over
  without an SES receipt rule / inbound queue, so there is nothing to drain.
- SES webhook ingress is unaffected and healthy: `drain_sqs_ingress
  ses-webhooks --once` returned `received=0 deleted=0 failed=0` against the
  live queue, scheduler heartbeat age was 14s, and provider events flowed the
  same day. Inbound SQS and its DLQ sit at 0/0/0.

**Owner statement:** nobody. This is an intended-disabled path, not an
orphan. The mailbox feature (`InboundAddress`/`InboundMessage`, console,
`provision_inbound_addresses`) exists for a future tenant-facing offering and
stays dormant until all of the re-enable checklist is done:

1. Create the SES receipt rule writing raw MIME to S3 + SQS (production
   prefix, new rule — never resurrect retired AWS rules under another name).
2. Set `SQS_INBOUND_EMAIL_QUEUE_URL` on the relay service.
3. Add one `drain_sqs_ingress inbound-email` container (same image, same
   pattern as `ses-ingress`) — the single future owner. Never run an AWS-side
   consumer next to it: one queue, one consumer.
4. Provision addresses via `provision_inbound_addresses`, then verify with one
   synthetic message to a provisioned address (not user mail) and confirm the
   `inbound_messages` row plus SQS delete.

## 2. Task-result pruning: owned by the scheduler, daily

`django_tasks_database_dbtaskresult` is derived worker-receipt data and grows
without bound: **33,180 rows / 68 MB in the first three weeks on Contabo**
(~1.6k rows/day). The upstream `prune_db_task_results` command existed but
nothing invoked it, and the retired AWS EventBridge prune rule referenced a
host script that was never implemented here.

**Owner statement:** `run_relay_scheduler` — the existing 15-second loop and
already-monitored heartbeat — runs `jobs.services.prune_finished_task_results`
once per UTC day: all queues (`queue_name="*"`), finished results older than
**14 days** (successful and failed alike). 14 days matches the upstream
default and the longest SQS redelivery horizon, so a receipt lives as long as
the queue could still redeliver its message. Steady state is ~22k rows
(~45 MB). The sweep is idempotent and never raises: failures are logged and
retried the next day, so hygiene can never break scheduling.

**Explicitly never pruned by this sweep:** `email_events`,
`campaign_recipients` (suppression/unsubscribe state — legal/compliance
weight), contacts, subscriptions, callback outboxes, `jobs_*` schedule/job
rows. Retention for business data is a separate policy decision, not this
sweep. At 76 MB for `email_events` (Aug–Oct) and 275 MB total DB on a 242 GB
disk, there is no disk-pressure case for pruning business rows.

**No-dual-owner rule:** the scheduler is the single pruner. Do not add a
Coolify scheduled task or any AWS rule pruning the same table. Live check:
Coolify scheduled tasks on `relay-staging` are `[]`, root has no crontab
prune entry, and `relay_schedules` holds 0 rows.

## 3. How to verify (sanitized; values never printed)

- Heartbeat + schedules: `record_scheduler_heartbeat` age via watchdog
  (`relay_scheduler_heartbeat`, expect seconds, not minutes);
  `SELECT count(*) FROM relay_schedules;` (0 is normal — the scheduler owns
  sweeps, not cron rows).
- Ingress no-op: `drain_sqs_ingress ses-webhooks --once` (expect
  `received=0 deleted=0 failed=0` on a quiet queue); `drain_sqs_ingress
  inbound-email --once` must fail `ImproperlyConfigured` while §1 holds.
- Prune preview (deletes nothing):
  `prune_db_task_results --queue-name "*" --dry-run` (2026-10-23: 9,684 with
  14-day retention). After deploy, the scheduler log line
  `task_results_pruned=True` appears at most once per UTC day.
- Growth watch: top-table sizes plus
  `SELECT count(*), min(enqueued_at), max(enqueued_at) FROM
  django_tasks_database_dbtaskresult;` — revisit retention if the steady
  state stops matching §2 math.
