# AC9b BOT webhook proof recheck — 2026-11-03

Intake: `58e9e81d-fc63-46f6-a7d6-693d6a8375a5`.
Parent: `b62f7763-f5ad-406c-8261-aa0fe4938219`.
Status: **unproven; intake remains open**. No DONE claim.

This recheck runs from a fresh worktree (`drain-58e9e81d`) on current
`origin/main` (`36eab79`). It performs no live email, no AWS call, no
application mutation, and prints no secret values.

## Prior evidence (unchanged)

- `docs/audits/2026-10-29-ac9b-bot-webhook.md` (commit `b56ef31`, still
  unpushed in worktree `relay-wt-58e9e81d`) and the Hetzner report
  `/home/alexey/tmp/agent-reports/relay-ac9b-bot-webhook-proof-2026-10-24.md`
  record: exactly one outbound test email was accepted; zero inbound
  messages, zero BOT deliveries, zero webhook POSTs followed.
- The saved receipt-rule snapshot excluded the temporary mailbox
  (`bot-test-ac9b@aishippinglabs.com`, ID 21, disabled), so the single
  permitted test email could not reach Relay ingestion.

## This run's findings

- No prod SSH access exists in this execution environment (no SSH keys or
  host configuration present), so the read-only service/DB recheck from
  2026-10-29 could not be repeated here. No remote connection was attempted.
- The acceptance criteria's ONE test email is already consumed. A retry
  requires explicit human authorization plus a confirmed receipt-rule
  accepted recipient; neither is present in this run. No email was sent.
- Receipt-rule correction would be an AWS change, which this intake forbids.

## Validation

- `uv run pytest mailing/tests/test_inbound_routing.py -q`: **23 passed in
  ~6s**, covering authenticated webhook payload delivery recording (HTTP
  2xx), allowlist enforcement, retries, redirect refusal, and audited
  provisioning. These mocked-HTTP tests validate the code path only; they
  cannot replace the missing live email/webhook evidence.
- `git diff --check` passed.

## Remaining work (needs Alexey decision)

1. Authorize one additional test email and confirm an accepted recipient
   (address covered by the live SES receipt rule), or authorize a
   receipt-rule change through the proper channel (outside this intake).
2. Set up the controlled throwaway receiver, capture the authenticated JSON
   payload, and record Relay message ID + inbound delivery ID + HTTP 2xx.
3. Disable (not delete) the temporary mailbox and stop the receiver, with
   audited manifest/config changes.
