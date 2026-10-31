# Block sender verification — intake 6fb1e509

Independent continuation review by Codex on 2026-10-29 at 14:06 UTC,
in the designated worktree `/home/alexey/git/relay-wt-6fb1e509`.
Implementation author: prior session `relay-block-link-6fb1e509`.
Reviewed implementation: `c28ae7d788bff301910707583a011e3b53cfe223`;
worktree HEAD: `d2a9eb49f17993e8ff40329914484fa74beb1454`.

## Local evidence

`uv run pytest mailing/tests/test_block_links.py -q`: **13 passed**.

`uv run pytest mailing/tests/test_inbound_routing.py mailing/tests/test_inbound_email.py mailing/tests/test_aisl_ses_forwarder.py mailing/tests/test_block_links.py -q`:
**72 passed**.

Code review confirms ingest checks original From and Reply-To before storing
body parts or creating deliveries. Tests exercise address/domain blocks and
absence of stored body and deliveries. Signed links expire after 30 days,
use separate block/unblock salts, and mutate rules only on POST. The tests
cover tampering, expiry, idempotence, protected domains, footer MIME content,
and unblock. No paid service or classifier is introduced. The earlier intake
reports no contact form feeding this route; that external fact was not
independently verified here.

The initial review found that `inbound_routing.sender_blocked()` rechecked
only the persisted From address for a pending delivery. After coordination
with the implementation session, this continuation fixes that gap by reading
the original From and Reply-To headers when block rules exist. A storage
failure logs only its exception type and preserves dispatch; initial ingest
already checked both headers. This is a best-effort recheck before creating
the outbound row, not cancellation of already queued outbound mail.

The block-link tests now pass **16 cases**, including late address/domain
Reply-To blocks and storage failure with log redaction. Ruff passes on both
changed Python files. The original implementation author independently
reviewed the diff, approved it, and reported **39 passing tests** across
block links and inbound routing (aplexer message
`01a187fe-b96e-76b0-9021-410976459d71`).

Full validation after the fix: `uv run pytest -q` completed with
**1764 passed, 13 skipped in 396.36 seconds**.

## Live read-only evidence

- [Implementation CI](https://github.com/DataTalksClub/relay/actions/runs/40867476866)
  and [development deployment](https://github.com/DataTalksClub/relay/actions/runs/40867476901)
  both report success for `c28ae7d`.
- GET `https://dev.relay.datatalks.club/inbound/block/intake-6fb1e509-invalid/`
  returns HTTP 404 with the new `Block sender | Relay` page.
- The same path on `https://relay.datatalks.club` returns HTTP 404 with
  Django's generic Not Found page. This is not proof of the feature being live
  there.
- [Promotion run 40868929705](https://github.com/DataTalksClub/relay/actions/runs/40868929705)
  remains **waiting**. Its `select` job succeeded; `promote` is waiting at the
  GitHub environment named `production`. The pending-deployment API names
  `alexeygrigorev` as reviewer. No approval or bypass was attempted.

The workflow/environment names above identify existing repository resources;
they do not establish a Datamailer production environment.

## Remaining acceptance evidence

Keep the intake open. There is no DONE claim.

1. Obtain the human approval required by the existing promotion workflow and
   verify the resulting deployment.
2. Verify an isolated route such as
   `blocktest-6fb1e509@aishippinglabs.com` forwards only to a test sink, never
   Alexey's inbox. The earlier proposed SES simulator route is a plan, not
   evidence that the route exists.
3. Send a real test message, obtain its signed footer link, verify GET has no
   effect, POST the block, then send a second message. Capture its blocked row,
   absent body storage, and absence of forwarding.
4. Once the feature is live, create and verify address and domain blocks for
   `stefan.s@tesseraleadsteam.com` and `tesseraleadsteam.com`, and report to
   Orin/Developer through an authorized channel.

No test mail, live routing mutation, or real-sender block was performed in
this continuation. No AWS profile or static key was used.

## 2026-10-31 follow-up: AC6 isolated-route proof is blocked on receiving policy

Prod promotion completed after the review above: Alexey removed the
`production` environment's required reviewer (rule t2070u) and dispatched
promotion run 41189277148, which shipped image `20261030-131611-858e94c`
(main `858e94c`, including `c28ae7d` and `57c0cfc`) to
`relay.datatalks.club`. Re-verified live on 2026-10-31: GET
`/inbound/block/intake-6fb1e509-invalid/` returns HTTP 404 with the
feature's own `Block sender | Relay` page (not Django's generic 404).

The implementing session (`relay-block-link-6fb1e509`) then performed a live
proof at 2026-10-31 09:40–09:45 UTC, but by temporarily repointing the real
mailbox `test@aishippinglabs.com` (Relay mailbox 17) to
`success@simulator.amazonses.com` and restoring it afterwards: SES receipt
rejected a brand-new test address with 550, so no isolated recipient
existed. Developer declined this as AC6 evidence because the brief requires
an isolated route and forbids touching real routes.

This continuation re-examined that constraint read-only and confirmed the
isolated-route requirement is currently unsatisfiable on prod:

- The only SES receiving path into Relay prod is the receipt rule in
  `aws-infra/main/aisl/email.tf` (account 387546586013, eu-west-1), whose
  `recipients` list is exactly `alexey`, `team`, `content`, `welcome`,
  `admin`, `test` at `aishippinglabs.com`. `main/relay/email.tf` is
  sending-only by explicit comment; `main/dev` has no inbound path; the
  sandbox root receives only its own `inbound.relay.dtcdev.click`.
- Independent SMTP probe on 2026-10-31 (RCPT only, QUIT before DATA, no mail
  sent) against `inbound-smtp.eu-west-1.amazonaws.com`:
  `ac6-blocktest-20261031@aishippinglabs.com` → `550 5.1.1 Requested action
  not taken: mailbox unavailable`; `test@aishippinglabs.com` → `250 Ok`.
- The production database (read-only SELECT) shows every SES-accepted local
  part is an enabled real route: mailbox 12 `alexey`, 13 `team`, 14
  `content`, 15 `welcome` forward to `alexey@datatalks.club`; 16 `admin` and
  17 `test` forward to Alexey's Gmail address. Message 559 (SES abuse
  complaint forwarded on `content@`) proves these carry real traffic.
  Mailbox 24 `blocktest-6fb1e509` (left by the implementing session) is
  disabled and cannot receive. Default `*` rows are discard.
- Therefore creating an isolated, clearly-named test route that can actually
  receive mail requires adding a recipient to the SES receipt rule — a
  change to the MAIN AWS account, which session rules forbid without
  Alexey's explicit approval, and no such approval is recorded for this
  intake.

The prior session's non-isolated evidence stands verified in the production
database: inbound message 562 received 09:40:16Z and forwarded (delivery 15,
outbound message 15209, SES id `010201a19154d37f-…-000000`, delivered to the
simulator sink); messages 563 and 564 (09:41:17Z/09:41:18Z, the second from
a different From with a blocked Reply-To) filed as `blocked` with no body
columns and no deliveries; the block rule was created by POST and removed by
the signed unblock link, leaving only the AC7 rules (blocked_senders 2–5).
The forwarded footer itself is not in the `transactional_messages` row
(both body columns are empty — forward MIME is assembled from the stored raw
message at send time), so the footer remains attested by the implementing
session's observation rather than re-derived here.

To unblock AC6, one of the following human decisions is needed:

1. Approve adding one dedicated recipient (for example
   `ac6-blocktest@aishippinglabs.com`) to `email_forward_mapping` in
   `aws-infra/main/aisl/email.tf` plus a matching enabled Relay mailbox
   pointing at `success@simulator.amazonses.com`. After the Terraform apply,
   the full isolated-route proof (forward with footer link, POST block,
   second mail dropped, cleanup) can run without touching any real route.
2. Explicitly sanction the time-boxed, audited repoint of
   `test@aishippinglabs.com` as acceptable AC6 evidence, in which case the
   prior session's 2026-10-31 proof (verified above) already covers the
   chain and only the isolation objection needs waiving.

This continuation wrote nothing to production: no route, rule, or mailbox
rows were created, changed, or deleted; no mail was sent; access was limited
to read-only database SELECTs, one RCPT-only SMTP probe, and public GETs.
No AWS profile or static key was used.
