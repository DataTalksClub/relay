# Relay deployment

Relay development runs at `https://dev.relay.datatalks.club` in AWS account
`387546586013` in `eu-west-1`, with its Terraform in
`DataTalksClub/aws-infra/main/dev`.

The old sandbox host `relay.dtcdev.click` (`i-03b7cd5de0a10a889`, sandbox
account `817685572750`) had its push-to-main auto-deploy retired on
2026-10-05 during the dtcdev→Relay transfer (the failed run `37253575983`
stays preserved as the record of why). That run's `InvalidInstanceId` is
historical evidence about the SSM target at that moment, not a statement
that the host is stopped: the same unchanged instance was later
independently observed healthy and serving `relay.dtcdev.click`. The manual
dispatch of `Deploy Relay Sandbox` remains, so any use of the retired
auto-deploy is a conscious choice. The sandbox sections below describe that
retired deployment surface.

## Development deployment

The `Deploy Relay Development` workflow in
[`.github/workflows/deploy-development.yml`](../.github/workflows/deploy-development.yml)
runs the full test gate, then deploys an exact commit to the development
host. Push to `main` (ignoring `docs/**` and `*.md`) and manual
`workflow_dispatch` both trigger it; runs serialize in the `relay-development`
concurrency group and never cancel each other.

| Aspect | Value |
|---|---|
| Region | `eu-west-1` |
| Account | `387546586013` (main) |
| Host | one private `t4g.micro` ARM64 instance in the website VPC, reached through SSM |
| Public entry | `https://dev.relay.datatalks.club` behind the dev CloudFront/ALB path on port 8000 |
| Database | shared dev RDS Postgres `dtc_relay_dev` via `RELAY_DATABASE_URL_SECRET_ARN` |
| Queues | one dev-owned pair, `ses-webhooks` and its DLQ; no sandbox queue drains |
| Logs | CloudWatch group `/relay/dev/host` |
| Secrets | Secrets Manager containers only; the deploy never populates values |

The deploy assumes the role in the `RELAY_DEPLOYMENT_DEPLOY_ROLE_ARN`
repository variable through the `development` GitHub environment and sends
one SSM command to `RELAY_DEVELOPMENT_INSTANCE_ID`. Both variables are
required — a missing variable fails the deploy before any AWS call. There are
no committed fallback instance ids or role ARNs. The variables are set only
from applied `main/dev` Terraform output evidence archived on aws-infra#73.

The host checks out the exact `GITHUB_SHA` and runs
`scripts/deploy_relay_sandbox.sh --environment development <sha>`. The
development branch of that script:

- fetches `SECRET_KEY` and `DATABASE_URL` from Secrets Manager and fails
  loudly if either is missing;
- pins `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` to
  `dev.relay.datatalks.club` (dev `infrastructure.env` does not carry them);
  `PUBLIC_BASE_URL`, `DEFAULT_FROM_EMAIL` (`relay-dev@datatalks.club`),
  `AWS_SES_*` and `RELAY_EMAIL_SEND_ROLE_ARN` flow from
  `infrastructure.env`;
- also adds the private in-VPC API host to `ALLOWED_HOSTS` when
  `infrastructure.env` exports a `RELAY_INTERNAL_URL` key — the DTC website
  calls Relay at that URL (`aws-infra main/dev` `website_service.tf` sets the
  website's `RELAY_BASE_URL` to it), and Django would see that hostname as
  the request Host. The audited `main/dev` revision does not export the key
  yet, so until the infra operator adds it, only the public hostname is
  allowed and the website's private calls would be rejected with `400`.
  The value is never wildcarded; host names only;
- starts no PostgreSQL and no Caddy: gunicorn binds `0.0.0.0:8000` for the
  shared ALB target, and public TLS terminates on the CloudFront/ALB path;
- gives every container an explicit `--memory` limit (the production limits
  table) because the host has 1 GiB plus 2 GB swap;
- drains only the dev-owned `ses-webhooks` queue; no `inbound-email` and no
  `relay-sandbox-*` drains;
- provisions the `datatalksclub`/`dtc-courses` client scope and registers the
  generated `RELAY_BOOTSTRAP_API_KEY` as `development-deployment`, without
  setting client senders;
- never writes `RELAY_DRY_RUN` — the variable stays operator-owned, and no
  email may be exercised on the development host. Enforced dry-run
  (relay#43) must be on the deployed head before any development worker
  starts.

The release gate is the same as everywhere: `/health/ready` answers, every
required container runs, and a `system.echo` task completes through the
worker. The workflow then polls
`https://dev.relay.datatalks.club/health/ready` from outside.

### Roll back the development host

Pick a known-good commit, then dispatch `Deploy Relay Development` for that
ref (or run the same script through SSM):

```bash
git -C /opt/relay fetch origin <commit>
git -C /opt/relay checkout --force <commit>
bash /opt/relay/scripts/deploy_relay_sandbox.sh --environment development <commit>
```

## Sandbox deployment (retired)

Relay runs at `https://relay.dtcdev.click` in AWS sandbox account
`817685572750` in `eu-west-1`, and we keep its Terraform in
`DataTalksClub/aws-infra/sandbox/relay`.

## Release checks

The GitHub Actions workflow deploys a commit only after lint, migrations,
Django checks and the full test suite pass. It sends one SSM command to EC2
instance `i-03b7cd5de0a10a889`.

The host builds one image and starts these containers:

- `relay-postgres`
- `relay-web`
- `relay-worker`
- `relay-scheduler`
- `relay-ses-ingress`
- `relay-inbound-ingress`
- `relay-cmp-callbacks`
- `relay-recipient-imports`
- `relay-caddy`

The deploy script checks every container, calls `/health/ready`, submits a
`system.echo` task and waits for the worker to finish it. A missing or broken
worker therefore fails the release.

## Check the host

Use Systems Manager instead of SSH:

```bash
aws ssm start-session \
  --region eu-west-1 \
  --target i-03b7cd5de0a10a889
```

List the running release:

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
git -C /opt/relay rev-parse HEAD
curl -fsS https://relay.dtcdev.click/health/ready
```

Docker sends each container's logs to the `/relay/sandbox/host` CloudWatch log
group.

Read recent logs on the host:

```bash
docker logs --since 15m relay-web
docker logs --since 15m relay-worker
docker logs --since 15m relay-scheduler
```

## Persistent and secret data

The encrypted EBS volume stores Postgres under `/var/lib/relay/postgres` and
Caddy state under `/var/lib/relay/caddy-data`. Replacing an application
container doesn't remove either directory.

The host creates `/etc/relay/runtime.env` once with mode `0600`. It contains the
Django secret, Postgres password, status token and sandbox client API key. The
deploy script reuses that file and never prints its values.

Terraform writes non-secret AWS resource names and the task-scoped email IAM
role ARN to `/etc/relay/infrastructure.env`.

## Roll back

Pick a known-good commit from the Relay repository, then run the same deploy
script through SSM:

```bash
git -C /opt/relay fetch origin <commit>
git -C /opt/relay checkout --force <commit>
bash /opt/relay/scripts/deploy_relay_sandbox.sh <commit>
```

Don't delete `/var/lib/relay/postgres` during a rollback. Ask for a database
restore decision if a migration changed data incompatibly.

## Verify email before moving a client

Confirm the shared sending identity in `us-east-1`, the inbound identity in
`eu-west-1`, and Relay's task role before sending:

```bash
aws sesv2 get-email-identity \
  --region us-east-1 \
  --email-identity dtcdev.click

aws sesv2 get-email-identity \
  --region eu-west-1 \
  --email-identity inbound.relay.dtcdev.click

aws iam get-role --role-name relay-sandbox-email-send
```

Relay uses `relay@dtcdev.click` as its default sender, while the `dtc-courses`
client uses the `courses` sender ID, which resolves to
`courses@dtcdev.click`.

Submit an email and wait for the transactional message to reach `sent`. Then
confirm the SES event reaches `relay-sandbox-ses-webhooks` and drains without a
DLQ message before you update a client to use Relay.
