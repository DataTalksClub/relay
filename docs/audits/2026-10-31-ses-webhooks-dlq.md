# SES webhooks DLQ episode — intake e30c9b48

Investigation by worker session `task-hub-3e78a3ad` on 2026-10-31 in the
designated worktree (branch `relay-wt-e30c9b48`, based on `origin/main`
858e94c). Alert: `ses_sqs_dlq_nonempty`, queue
`relay-production-ses-webhooks-dlq`, depth 1, fired 2026-10-31T04:58:58Z by
the relay transactional monitor running in image `20261028-113911-fd1e1f7`
(source sha `fd1e1f7`). The DLQ is drained by no one; it exists so failed
ingress messages can be inspected and redriven by hand.

## Outcome

- Not an application failure. The SES webhook ingress processed every
  message cleanly across the whole 14-day log window, including the minutes
  around the alert. No handler exception, failed poll, or crash was logged.
- No mail was lost. Every SNS-published SES event in the incident cohorts
  reconciled exactly (durable-ingress received == persisted ==
  sns_published; window 04:30–05:00 reads 4 == 4 == 4).
- The DLQ emptied itself within the hour: the monitor, which re-checks depth
  every scheduler pass (15 s) and would have re-alerted once its 1-hour
  in-process dedupe expired, never re-alerted; the 09:35:56Z redeploy (fresh
  scheduler process, dedupe reset) also produced no alert. Monitor
  heartbeats report `alerts=0` through 16:00Z.
- This session performed no redrive, purge, or delete. The only redrive-like
  signature is an ingress receive at 05:14:20Z whose
  `process_ses_webhook_event` task succeeded at 05:14:21Z — consistent with
  a manual `start-message-move-task` by the on-call human, who was actively
  investigating at the time (intake `oncall-relay-dlq-20261031T0459` created
  04:59:36Z).

## Timeline (UTC; log evidence from VictoriaLogs on contabo-a)

Vector has shipped all container logs from both Contabo hosts to
VictoriaLogs since 2026-10-22 (14-day retention), so the incident is fully
reconstructable without AWS console access.

| Time | Event |
|---|---|
| 04:40:44–04:46:31 | `ses-ingress-fjnzwomg8n42lztloo34just` (image `20261028-113911-fd1e1f7`, the only consumer of `relay-production-ses-webhooks`) received four messages and deleted all four: `received=1 deleted=1 failed=0` |
| 04:46:31–04:58:43 | Queue idle. Ingress idle heartbeats (`idle_polls=30` at 04:56:32) prove zero receives since 04:46:31. The scheduler's transport guard checked DLQ depth every 15 s pass and saw 0 each time; no `ses_monitor_transport_query_failed` line exists in the window |
| 04:58:58 | Monitor fires `relay_ses_guard_alert {"kind": "ses_sqs_dlq_nonempty", "depth": 1, ...}` — the only guard line in the window. Depth became 1 between 04:58:43 and 04:58:58 |
| ~05:14:20 | Ingress received one message (`received=1 deleted=1 failed=0`); provider-events task `e0f47ccb…` (`mailing.tasks.process_ses_webhook_event`) SUCCEEDED at 05:14:21 — consistent with a manual redrive landing back on the source queue |
| ≤05:58:58 | Depth back to 0: no re-alert at the dedupe expiry (05:58:58), none after the redeploy-restarted scheduler (09:35:56Z), `alerts=0` in every subsequent monitor heartbeat |

Deployment context: production (Coolify project `dtc-prod`, service
`relay-staging` on contabo-b) ran `fd1e1f7` from the container recreate at
2026-10-28T22:35Z until the 2026-10-31T09:35Z deploy of `858e94c`. The whole
episode happened under `fd1e1f7`, matching the alert metadata.

## Why the application path is excluded

The DLQ is reachable only through the SQS redrive policy (`maxReceiveCount=5`,
source visibility timeout 180 s, per `aws-infra` `main/relay/messaging.tf`):
a message must be received five times without being deleted. At `fd1e1f7`
every application-side failure on that path logs somewhere docker captures:

- a handler exception logs `ERROR … SQS message <id> failed (<class>): <reason>`
  (`mailing/sqs.py::process_sqs_event`, sanitized since fd1e1f7);
- a failed poll logs `received=N deleted=0 failed=N` (drain command stdout);
- a handler crash outside the per-message try logs
  `SQS worker handler failed …` (`mailing/sqs_worker.py`);
- a receive-API error crashes the drain command with a stderr traceback;
  a container restart would appear in the docker daemon journal.

Queries over 2026-10-17T00:00Z→2026-10-31T04:58Z found none of these:
zero `SQS message … failed` lines from any container, zero polls with
`failed≠0` or `deleted<received` for the ingress, no ses-ingress restarts or
OOM kills in the docker journal between the Oct 28 22:35Z recreate and the
Oct 31 09:35Z redeploy, and the ingress process demonstrably alive and idle
across the final minutes. No other container on either Contabo host drains
this queue (compose defines one drain per queue; contabo-a hosts no
ses-ingress at all). The five receives that moved the message into the DLQ
therefore did not come from the application.

The most consistent explanation is receives issued outside the application —
for example repeated manual `aws sqs receive-message` peeks of the source
queue, where the queue-default 180 s visibility holds the message invisibly
between peeks and the fifth receive (~04:58:45) trips `maxReceiveCount` —
followed by a manual redrive around 05:14. Identifying the actor requires
AWS CloudTrail for account 387546586013, which this session is not
authorized to read.

Reconciliation independently rules out an SES cohort gap:
`relay_ses_ingress_reconciled` lines cover every rolling window around the
incident with `received == persisted == sns_published`, and no
`relay_ses_gap_pending` was open. A message stranded in the queue or DLQ
would by definition be missing from the durable-ingress ledger and would
have shown up as published > received.

## Relation to ses_sns_persisted_gap (2026-10-27 intake)

Unrelated. That intake concerned a 1-of-29 publication cohort gap on Oct 27.
This episode has no gap: all incident cohorts reconciled exactly, and the
two monitors observe different layers — the persisted-gap check counts
events durable ingress already accepted, while a DLQ entry never reaches
that ledger.

## Decision

No code bug exists in the SES webhook path at `fd1e1f7`; retry and DLQ
mechanics worked as designed and no normalize/enqueue failure ever fired.
No fix or regression test is required. No purge or redrive was performed by
this session and none is needed — the DLQ has been empty since at latest
05:58:58Z. The message's SES event id and receive count (intake AC1) can no
longer be read from the queue because the queue is empty; confirming the
AWS-side receive actor is an operator task via CloudTrail if desired.

## Notes for future on-call

- The transport guard (`mailing/services/ses_monitor.py::
  check_ses_transport_health`) reads DLQ depth every scheduler pass (15 s
  default interval) and dedupes alerts per kind for one hour in-process; a
  scheduler restart resets the dedupe. Silence across a restart is strong
  evidence the DLQ is empty.
- VictoriaLogs on contabo-a (host network, port 9428, 14-day retention,
  stream fields `host,container_name`) reconstructs relay incidents without
  AWS access. LogsQL note: underscore-joined log tokens (e.g.
  `relay_ses_guard_alert`) match only as whole tokens; filter on hyphenated
  fragments (e.g. `dlq`) or exact tokens.
- Redrive runbook, if a future episode is a real application failure:
  `aws sqs start-message-move-task --source-arn <dlq-arn>
  --destination-arn <source-queue-arn>`; the DLQ's redrive-allow policy
  already permits the paired ses-webhooks queue.
