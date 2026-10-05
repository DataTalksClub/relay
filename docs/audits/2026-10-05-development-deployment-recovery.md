# Development deployment recovery — 2026-10-05

Audit receipt for relay#42. Offline record of the sandbox retirement decision
and the replacement development deployment path. No AWS calls, workflow runs,
or live verifications were performed while writing this; live gates are
tracked on the issue.

## What happened

- Accepted Relay main `925286f4365887e6ff4663fbf529be2cbe758bfc` pushed to
  GitHub; CI test succeeded
  (run `37253575966`, job `111585956863`).
- `Deploy Relay Sandbox` (run `37253575983`) failed in its deploy job: the SSM
  `send-command` to the sandbox target returned `InvalidInstanceId` at
  `2026-10-05T02:05:29Z`. Public health verification was skipped. The run is
  preserved and must never be rerun, retried, or cancelled.

## Owner decision (2026-10-05)

- **Auto-deploy retirement confirmed (failure branch E).** The chosen
  retirement covers the sandbox's push-to-main auto-deploy, not the host
  itself: nothing observed in this issue shows `i-03b7cd5de0a10a889` /
  `relay.dtcdev.click` stopped. No reconciliation of that target is
  attempted from this repository; the failed run stays as the record.
- **Replacement development target.** The dedicated main/dev Relay surface
  `https://dev.relay.datatalks.club` — main account `387546586013`, workload
  region `eu-west-1` — is the development deployment target, per the
  committed aws-infra `main/dev` contract at
  `f40e3a13798bce488a4d0b7822361cccbb24aa80`.
- **Production untouched.** `relay.datatalks.club`, `main/relay`,
  `deploy-prod.yml`, and all `RELAY_PRODUCTION_*` variables are out of scope.

## Later same-day observation (root-provided; no calls made here)

The sole OnCall terminal independently verified commit
`c5bddf15860853d54242eac433952d96b5f8eca3` against the unchanged old sandbox
target `i-03b7cd5de0a10a889` (account `817685572750`): CI run `37308443576`
and `Deploy Relay Sandbox` run `37308443614` both succeeded, SSM returned
`Success`, and `https://relay.dtcdev.click` reported ready (UX report
`.tmp/ux-main-integration-20261005/oncall.md`, receipt `41#5994382735`).
The earlier `InvalidInstanceId` therefore reads as a historical episode, not
as current host state. This section records root's message only — no AWS,
CI, or workflow observations were made while writing this audit. The
dedicated main/dev recovery (this issue's target) remains pending.

## What changed in this repository

- `deploy-sandbox.yml`: the `push` trigger was removed. The manual
  `workflow_dispatch` remains so touching the retired sandbox auto-deploy is
  always a conscious choice. Nothing else in the file changed.
- New `deploy-development.yml`: same four-step test contract as the sandbox
  workflow, then a `deploy` job gated on the `development` GitHub environment
  and the required `RELAY_DEPLOYMENT_DEPLOY_ROLE_ARN` /
  `RELAY_DEVELOPMENT_INSTANCE_ID` repository variables (no committed
  fallbacks), one SSM command deploying the exact `GITHUB_SHA`, and public
  health verification at `https://dev.relay.datatalks.club/health/ready`.
- `scripts/deploy_relay_sandbox.sh`: a third `--environment development`
  branch implementing the dev runtime contract (no PostgreSQL/Caddy, RDS via
  Secrets Manager, `0.0.0.0:8000` bind, per-container memory limits, only the
  dev-owned `ses-webhooks` drain, `ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS`
  pinned to `dev.relay.datatalks.club`, `RELAY_BOOTSTRAP_API_KEY` client
  bootstrap without senders, log group `/relay/dev/host`). The script never
  writes `RELAY_DRY_RUN`; sandbox and production branches keep their
  semantics.
- `docs/relay-deployment.md` and `README.md` document the development target
  and mark the sandbox auto-deploy retired with the 2026-10-05 decision
  date, distinguishing that chosen retirement from the host's observed
  deployment state.

## Private API host seam (root evidence, 2026-10-05)

The committed aws-infra `main/dev` at `f40e3a13798bce488a4d0b7822361cccbb24aa80`
wires the DTC website to Relay privately: `website_service.tf` sets the
website's `RELAY_BASE_URL=local.relay_internal_url`
(`http://relay-dev.<website-internal-zone>:8000`), and its security groups
allow direct private HTTP to the dev Relay host. Django validates that
hostname as the request Host, so dev `ALLOWED_HOSTS` must eventually include
it alongside `dev.relay.datatalks.club`.

At that same revision the seam is missing: `user_data.sh.tftpl` writes
`/etc/relay/infrastructure.env` without any internal-URL key, and the zone
label lives in the website root's applied remote state, so the exact private
hostname cannot be derived offline without inventing it. The deploy script's
development branch therefore derives the host from a `RELAY_INTERNAL_URL`
key in `infrastructure.env` when the infra operator exports one (host names
only; wildcard or list values are ignored), and deploys cleanly without it.
Until the seam exists, the website's private calls would be rejected with
`400` — this must close before any DTC website promotion sequencing.

## Prerequisites still open before the first development deployment

1. aws-infra#73 applied evidence: the dev host instance id and the
   `repo:DataTalksClub/relay:…:environment:development` deployer role ARN,
   from applied `main/dev` output, plus SSM-`Online` confirmation. Repository
   variables are set from that evidence only.
2. relay#43 enforced `RELAY_DRY_RUN` landed on the deployed head — no
   development worker may boot without it, and no email may be exercised on
   dev in this issue.
3. One newly authorized exact-head deployment observed by the sole OnCall,
   with host-side SHA and container-set evidence.

Nothing here weakens a gate: the four-step test contract, the static
workflow/script assertions, and the single-deployment rule all carry over
from the groomed issue body.
