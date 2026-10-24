# Per-domain inbound routing

Relay receives SES S3-action notifications over SNS → SQS → its inbound ingress
and provider-events task lane. SES supplies the receipt recipients (including
Bcc); header recipients are used only by the legacy S3-event adapter. One stored
message can create deliveries for multiple mailboxes. Duplicate Message-ID
notifications reuse the stored message and its durable delivery outbox.

In Django admin, configure **Inbound domains** and **Inbound mailboxes**. A domain
selects the Relay outbound client and its provider-verified From address. Each
domain starts with exactly one `*` default mailbox, initially `discard`; change
that row to a BOT or FORWARD catch-all if required. Exact addresses take priority;
a disabled exact mailbox never falls through to the default. Unknown domains
continue through the existing receiving-address path. Disable domains/mailboxes
to retire them; historical records remain protected from deletion.

BOT mailboxes require an exact sender allowlist (empty denies all) and a
`webhook_secret_id`. The reference is either a Secrets Manager secret identifier
or `env:VARIABLE` naming a Coolify secret environment variable. The secret is JSON
with `url` (HTTPS), `header` (for example Authorization), and `value` (the complete
auth header value). Only the reference is stored in the routing tables or shown
in admin/API. Set/rotate the actual secret through Secrets Manager or Coolify,
never through a command-line argument, git, or ordinary routing JSON. Redirects
are refused. Error logs record numeric IDs, status codes and exception classes,
never URLs, auth values, response bodies, or exception messages.

BOT JSON contains `from`, `to`, `subject`, `snippet`, `message_id`, and
`message_url` (authenticated staff-only raw MIME download). The receiver should
deduplicate `Idempotency-Key: relay-inbound-<delivery id>`; HTTP cannot guarantee
exactly-once delivery after a receiver accepts but the response is lost. Any 2xx
succeeds. Five attempts use exponential delays of 30, 60, 120, 240 seconds.
Final failure emits ERROR and makes `/health/` return HTTP 503 with routing
status FAIL; overdue work also fails that health check. `/health/ready` remains
a process/database readiness check to keep operators able to inspect failures.
Failures remain visible under **Inbound deliveries** and the admin deliveries API.

FORWARD deliveries create a normal Relay TransactionalMessage, queue it through
Relay's existing task pipeline, and send through `mailing.ses`, including its
provider routing, dry-run enforcement, retry and event logging. The raw MIME is
read from the protected stored message at send time, keeping multipart bodies,
binary/inline attachments, and charsets intact. From is rewritten to the domain's
verified address with the original display name; Reply-To is the original
sender. Date/Message-ID/References/In-Reply-To/Subject are retained as
X-Original-*; stale transport/authentication headers and Bcc are removed.

Loop controls reject X-Loop/X-Relay-Inbound, automatic replies, Relay-originated
message IDs and domain senders, bounces and calendar replies. Auto-generated
alert emails can pass the allowlist once; forwarded mail is marked auto-forwarded
and X-Loop. Original senders are excluded from forward destinations. Each
mailbox has a serialized per-minute cap; refusals are recorded. DMARC messages
are retained in raw storage and suppressed from forwarding, preserving the old
archive-only behaviour without running the forwarding Lambda.

DMARC archive equivalence (migration difference, documented not hidden): the
legacy Lambda saved a second copy of each DMARC report to the separate
`<domain>-dmarc-reports` bucket (versioned, transition to STANDARD_IA at 30
days, expiry at 365 days) while the inbound `.eml` expired from the
`<domain>-ses-emails` bucket after 7 days. Relay keeps the single raw MIME in
the ingress bucket (same 7-day lifecycle) plus the durable `InboundMessage` row
and a `suppressed/dmarc_archived` delivery record, and never forwards or wakes
a bot for it. Behaviour parity (archive-only, no-forward) is implemented and
covered by `test_dmarc_report_archived_not_forwarded`. Retention parity (a
365-day second copy) is NOT claimed: reproducing it needs either (a) a
`dmarc/` prefix lifecycle rule on the ingress bucket or a copy to the legacy
DMARC bucket at ingest, plus runtime `s3:PutObject` IAM — a Terraform + IAM
change that is a REMOVAL/ADD proposal only until Alexey approves and the
authorized MAIN apply path exists. Until then, DMARC raw older than 7 days is
available only as the stored body/snippet/metadata row, not as raw MIME.

The `process_inbound_deliveries` worker owns dispatch and reconciliation. DEV
runs it with outbound sending disabled, and never drains a production queue.
Production's ingress stands by until its queue is configured. Provision routes,
secrets, S3 retention and runtime IAM before configuring that queue. Do not
activate receipt cutover until the deployed Relay path is proved. Keep the old
Lambda, role, permissions and Dapier resources until separately approved cleanup.

## Management API and CLI

Admin bearer keys authenticate these paths (client send keys do not):

- GET/POST `/api/admin/inbound-domains`; GET/PATCH `.../<id>`
- GET/POST `/api/admin/inbound-mailboxes`; GET/PATCH `.../<id>`
- GET `/api/admin/inbound-deliveries` (latest 100)

Domain fields: `name`, `client_id`, `from_email`, `enabled`. Mailbox fields:
`domain_id`, `local_part`, `kind` (`bot`, `forward`, or default-only `discard`),
`enabled`, `destinations`, `sender_allowlist`, `webhook_secret_id`,
`rate_per_minute`. API and Django admin changes append OperatorAudit records
containing changed field names, without secret values. Configure the required
admin key through the CLI's existing environment/config mechanism.

```
datamailer inbound domains
datamailer inbound domains --data-file domain.json
datamailer inbound mailboxes --id 1 --data-file mailbox.json
datamailer inbound deliveries
```

Only references belong in those JSON files. A default rule already exists after
creating a domain; PATCH its ID rather than creating another `*` mailbox.

## dtcdev.click bot wake targets (no new credentials)

`dev@`, `tofu@`, and `orin@dtcdev.click` are BOT mailboxes with the same
sender allowlist (Alexey's addresses plus `noreply@datatalks.club`,
`no-reply@dtcdev.click`, `alerts@dtcdev.click`). Their `webhook_secret_id`
values (`env:INBOUND_DEV_WEBHOOK`, `env:INBOUND_TOFU_WEBHOOK`,
`env:INBOUND_ORIN_WEBHOOK`) point at the existing authenticated dapier webhook
hooks for `dev`/`tofu`/`orin`, which run the mailbox workflows that file Hub
intake tasks for the matching project. No direct bot-destination URL/token
pairs exist in tofu/orin workflow storage; do not invent any. Set/rotate the
hook-backed JSON secrets through Coolify/Secrets Manager only. Keep the current
dapier `dev@` email wake intact until the Relay webhook path is proved live
with a 2xx for each of `dev@`, `tofu@`, and `orin@`.
