# Transactional retry runbook

Operator retry for failed transactional messages. This replaces the manual
shell workaround used for message 8017 on 2026-10-24 (`status failed->queued`
plus `enqueue_transactional_email(build_transactional_queue_payload(...))`
in a prod shell). Use the built-in retry everywhere now.

## Single message

CLI (Relay admin CLI, runs on the host):

```bash
python manage.py retry_transactional 8017 --reason "route fixed, resend"
python manage.py retry_transactional 8017 --dry-run
```

Admin API:

```bash
curl -X POST https://relay.example.com/api/admin/transactional-messages/8017/retry \
  -H "Authorization: Bearer <relay_admin_...>" \
  -H "Content-Type: application/json" \
  -d '{"reason": "route fixed, resend"}'
```

Admin UI: open the message in Email activity, use **Retry send** when the
status is `failed`.

What the retry does:

- keeps the same message row and idempotency key (no new message, no
  duplicate send of an already sent/delivered row — non-`failed` statuses
  need `--force`/`"force": true` plus a reason);
- re-resolves the sender at retry time, so a fixed sending-domain/route
  applies, then re-queues through the normal `enqueue_transactional_email`
  path (SES route selection still happens at send time);
- records a `queued` email event with `retry: true` and `retry_count` in
  message metadata (attempt history);
- writes one `operator_audits` row (`transactional_message.retry`, who /
  when / reason).

## Bulk retry by filter

List first, then confirm:

```bash
python manage.py retry_transactional --client hetzner-maintenance \
  --error-contains "MessageRejected" --dry-run
python manage.py retry_transactional --client hetzner-maintenance \
  --reason "route fixed" --yes
```

Other bulk filters: `--status` (default `failed`), `--template-key`,
`--created-after`/`--created-before` (ISO), `--limit` (1..100, default 100).

Admin API bulk:

```bash
curl -X POST https://relay.example.com/api/admin/transactional-messages/retry \
  -H "Authorization: Bearer <relay_admin_...>" \
  -H "Content-Type: application/json" \
  -d '{"client": "hetzner-maintenance", "error_contains": "MessageRejected",
       "reason": "route fixed", "dry_run": true}'
# then the real run needs "confirm": true
```

## Guardrails

- Max bulk batch is 100. Narrow the filters for more.
- Bulk CLI prompts `Type YES to continue` unless `--yes`; bulk API needs
  `"confirm": true` unless `"dry_run": true`.
- Suppressed (hard bounce / complaint) and blocklisted recipients never
  retry, even with `--force`.
- The retry enqueues through the normal worker path, so `RELAY_DRY_RUN=1`
  still suppresses the actual SES send downstream; the CLI/API surface
  whether dry-run is enforced.
