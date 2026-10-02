# AISL access on the deployed Relay

The integration target is `https://relay.datatalks.club/`. The Relay host is
`i-06ac3a57e95e32e4f` in AWS account `387546586013`, region `eu-west-1`.
The tenant contract comes from community-base `docs/plan/phase-6.md` and
AISL's `email_app/relay_sync.py`: organization `aisl`, audience `aisl`,
client `aisl-website`.

## Provision access

Run `bash scripts/provision_aisl_tenant.sh --dry-run` to inspect the plan.
Run the script without arguments on the deployed host through SSM to apply it.
It uses the running `relay-web` image, requires no application restart, and
preserves existing credentials on subsequent runs.

The host stores separate AISL credentials in `/etc/relay/aisl.env`, mode `0600`:

| Relay host variable | AISL deployment setting |
| --- | --- |
| `RELAY_AISL_API_KEY` | `RELAY_API_KEY` |
| `RELAY_AISL_WEBHOOK_SECRET` | `RELAY_WEBHOOK_SECRET` |

Transfer credentials through the site's deployment secret store. Keep values
out of issue bodies, agent messages, SSM command text and command output.
Configure `COMMUNITY_BASE["RELAY_BASE_URL"]` through the site's `RELAY_BASE_URL`
setting. A site using the private VPC endpoint must have the required network
rules; verify the endpoint from that site's runtime before switching backends.

The allowed job webhook origin is `https://aishippinglabs.com`. Sender IDs are
`noreply` (default), `content`, `welcome` and `team`, each mapping to the same
local part at `aishippinglabs.com`. Check these against the site's runtime
sender overrides before enabling mail.

## Integration checks still required

- Verify Relay's email task role permits the `aishippinglabs.com` SES identity
  and that the identity is verified in Relay's sending region. Existing Relay
  infrastructure scopes the role to `datatalks.club`; tenant provisioning
  alone does not grant permission to send as AISL.
- Agree on callback registration and receiver readiness with the AISL owner.
  The provisioner sets job webhook signing configuration; it does not enable
  a mail callback endpoint.
- Verify authenticated tenant access and a `system.echo` task from AISL's
  runtime, then test job ingress and mail callbacks before syncing schedules.
- Import templates and contacts, validate mail-link routing, and complete
  the cutover evidence described in community-base's phase 6 plan before
  changing `MAIL_BACKEND` or `JOBS_BACKEND` or retiring local workers/history.

Provisioning access starts integration. It does not complete the phase 6
traffic migration or its readiness gates.
