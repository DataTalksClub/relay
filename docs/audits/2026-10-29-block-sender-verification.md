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

Additional limitation: `inbound_routing.sender_blocked()` rechecks only the
persisted From address for a pending delivery. A Reply-To-only block added
after ingest is not covered by that recheck. Initial ingest does check both.
This finding was sent to the session holding the implementation edit scopes.

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
