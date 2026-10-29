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

### Blocking a sender from a forwarded message

The blocklist (`BlockedSender`, address or domain scope) is checked at ingest
against the original From **and** Reply-To addresses. A blocked message is
filed as a `blocked` row with no stored body, creates no deliveries and is
never forwarded. A FORWARD delivery still pending when a rule is added is
suppressed with reason `blocked_sender` instead of being sent.

Every forward ends with a short footer (appended to the first inline
text/plain and text/html parts; signed/encrypted or body-less mail gets a
separate inline part instead) with two links:

- **Block this sender**: the original Reply-To address, else From.
- **Block domain <domain>**: omitted for shared mail providers (gmail.com,
  outlook.com, yahoo.com, icloud.com, proton.me, ...; see
  `FREEMAIL_DOMAINS` in `mailing/services/block_links.py`), where only the
  address can be blocked.

No footer is added when the sender is at one of our own domains
(datatalks.club, aishippinglabs.com, dtcdev.click, every InboundDomain,
SendingDomain and client From domain, and their subdomains); those can never be
blocked from a link at either scope.

Links are Django `TimestampSigner` tokens (keyed by `SECRET_KEY`) over the
inbound message id, value and scope, valid for 30 days. No login is needed.
`GET /inbound/block/<token>/` only shows a confirmation page, so mail scanners
and link prefetchers cannot block anyone; the page's POST creates the rule
with `origin=email_link`, `reason=<original Message-ID>` and the source
message. Repeating it is a no-op ("already blocked"). The result page shows
the rule and a signed **Unblock** link (`/inbound/unblock/<token>/`, also
GET-confirm then POST), which deletes the rule. Tampered, foreign-salt or
expired tokens get a 404 page and change nothing.

Links point at `INBOUND_BLOCK_LINK_BASE_URL`, defaulting to the console host
from `AUTH_CALLBACK_URL` (relay.datatalks.club in production), not
`PUBLIC_BASE_URL`, because the public mail host only proxies the tracking and
unsubscribe paths. Rules created this way are also visible and removable in
the operator console's blocked-senders list.

There is no paid filter or classifier on this path, and no honeypot: no AISHL
web form feeds these mailboxes (the pitches are direct mail to
alexey@/team@/welcome@aishippinglabs.com).

Loop controls reject X-Loop/X-Relay-Inbound, automatic replies, Relay-originated
message IDs and domain senders, bounces and calendar replies. Auto-generated
alert emails can pass the allowlist once; forwarded mail is marked auto-forwarded
and X-Loop. Original senders are excluded from forward destinations. Each
mailbox has a serialized per-minute cap; refusals are recorded. DMARC messages
are retained in raw storage and suppressed from forwarding, preserving the old
archive-only behaviour without running the forwarding Lambda.

DMARC aggregate reports use the shared Relay database archive for every enabled
inbound domain. Detection ports the AISHL Lambda's sender substring, subject
regex and attachment heuristics (including decoded non-ASCII subjects). It runs
before loop/allowlist/default-discard handling, so reports neither forward nor
wake bots, even when the sender is blocked. A message addressed to two domains
has an archive for each domain; notification replays do not duplicate evidence.

`DmarcArchive` retains the complete raw `.eml`, `DmarcAttachment` retains the
original application zip/xml.gz/gz/xml bytes, and `DmarcReport`/`DmarcRecord`
store aggregate metadata, published policy, source IP/count, evaluated
DKIM/SPF/disposition, identifiers and authentication results. The sender-domain
layout follows the Lambda: `dmarc/<reporter-domain>/<timestamp>-<digest>.eml`
and `dmarc-zips/<reporter-domain>/<timestamp>-<digest>-<filename>`.
These are database evidence keys, not pointers into the seven-day ingress
bucket. The digest avoids the Lambda's same-second filename collision.
`retain_until` is at least 365 days after ingest; there is no automatic expiry
or deletion operation. DMARC admin/API access is read-only. Database backups
must include these binary fields; archive volume adds to database/backup size.

Compressed XML is bounded to 10 MiB decompressed, ZIPs to 32 members and
25,000 records per report. XML entity/DOCTYPE declarations are rejected and
ZIP paths are never extracted to disk. Invalid reports retain both raw and
attachment evidence with a parse-error class, emit a numeric-ID ERROR log,
and remain suppressed. Parsing never fetches external resources.

Read in Django admin under DMARC archives/attachments/reports/records, through
admin-key API `GET /api/admin/inbound-dmarc` (optional `?domain=...`) and
`GET /api/admin/inbound-dmarc/<id>` (including parsed rows), or CLI
`datamailer inbound dmarc [--id ID]`. Download the archived evidence using
`GET /api/admin/inbound-dmarc/<id>/raw` and
`GET /api/admin/inbound-dmarc/<id>/attachments/<attachment-id>`.
On the server: `uv run python manage.py dmarc_reports --domain aishippinglabs.com`
or `dmarc_reports --id ID`. No credentials or binary mail bodies appear in list
responses. Client send keys cannot access this operator API.

The old Lambda's versioned S3 bucket (IA at 30 days, expiry at 365) is unchanged.
An optional parity copy is not needed for durable retention and is not enabled;
new MAIN IAM/S3 access requires a separate Terraform proposal and Alexey's OK.

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
