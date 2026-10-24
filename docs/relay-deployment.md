# Relay deployment

Relay development runs at `https://dev.relay.datatalks.club` (Coolify
resource `relay-dev`, `relay_dev` database). Delivery is owned by the
`Deploy Relay Dev (Contabo)` workflow in
[`.github/workflows/deploy-dev.yml`](../.github/workflows/deploy-dev.yml):
push to `main` runs the test gate, builds `linux/amd64` into GHCR, and the
Coolify deploy API rolls the DEV service (web + DB workers + scheduler +
callback dispatchers; no `ses-ingress` — DEV has no SQS queue and must never
drain the production queue).

Production (`https://relay.datatalks.club`, Coolify resource `relay-staging`,
`relay` database) is promoted ONLY by the manually dispatched
`Deploy Relay Prod (Contabo)` workflow in
[`.github/workflows/deploy-prod.yml`](../.github/workflows/deploy-prod.yml):
it promotes one dev-proven release (the `dev-release` artifact of a
successful deploy-dev run on main, latest by default or a named tag for
rollback) through the `production` GitHub environment (required reviewer).
A push to `main` can never deploy production: deploy-dev.yml only knows the
DEV service UUID.

The old AWS EC2/SSM development path is retired. Its host
`i-0a3d2851cb906dc69` (main account `387546586013`, `eu-west-1`, Terraform
`DataTalksClub/aws-infra/main/dev`) was terminated on 2026-10-22 at 04:29 UTC
(CloudTrail `TerminateInstances` by `ml-in-action`; previous state `running`),
after the last green SSM deploy (run `39714002383`). The next push-to-main run
(`39761744485`) then failed with
`InvalidInstanceId: Instances not in a valid state for account` because the
repository variable `RELAY_DEVELOPMENT_INSTANCE_ID` still pointed at the
terminated instance. The variable was removed and the workflow below no
longer sends SSM commands, so this failure mode cannot recur. Note for
aws-infra: its `main/dev` root still declares `aws_instance.relay_host`, so
the next Terraform apply would try to recreate the host — that drift belongs
to aws-infra, not to this repo.

Recovery evidence checked on 2026-10-23:

- [Failed development run 39761744485](https://github.com/DataTalksClub/relay/actions/runs/39761744485)
  passed AWS credential setup, then failed `SendCommand` at
  `2026-10-22T10:18:37Z`. Its deploy log records the terminated instance,
  account `387546586013`, and `eu-west-1`, consistent with the cutover
  history above rather than an account or region mismatch.
- Commit `74ddb69ed6162bcc51327f1e6b67166f93085731` removed the SSM calls.
  The resulting [Deploy Relay Development run 39765630943](https://github.com/DataTalksClub/relay/actions/runs/39765630943)
  passed both tests and public development health verification. This was a
  health gate; image delivery was separately verified by the successful
  [Coolify run 39765630710](https://github.com/DataTalksClub/relay/actions/runs/39765630710)
  for the same commit.
- Commit `410bb4fd97783990d8cb3945511e4ddff3d408e7` consolidated delivery
  and development health verification into `deploy-dev.yml` and deleted
  `deploy-development.yml`. The current development workflow uses Coolify
  secrets and contains no AWS credential setup, instance variable, or SSM
  command. It does not require access to the retired AWS host.

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

The `Deploy Relay Dev (Contabo)` workflow in
[`.github/workflows/deploy-dev.yml`](../.github/workflows/deploy-dev.yml)
runs the full test gate, builds the release image, applies
[`deploy/coolify/docker-compose.dev.yml`](../deploy/coolify/docker-compose.dev.yml)
to the DEV service, and verifies the development entry point. Push to
`main` (ignoring `docs/**` and `*.md`) and manual
`workflow_dispatch` both trigger it; runs serialize in the `relay-dev`
concurrency group and never cancel each other.

| Aspect | Value |
|---|---|
| Delivery | `Deploy Relay Dev (Contabo)` workflow (GHA → GHCR → Coolify deploy API) |
| Public entry | `https://dev.relay.datatalks.club` served by the Contabo host |
| Release gate | Coolify `running:healthy` + `https://dev.relay.datatalks.club/health/ready` |
| Proven-release record | `dev-release` artifact (`image_tag`, `source_sha`, `dev_run_id`) |

The workflow needs no AWS credentials; it uses the `development` GitHub
environment and the `COOLIFY_RELAY_DEV_SERVICE_UUID` secret.

### Migration startup gate

The release image runs `scripts/container-entrypoint.sh` before every role's
command, including web, database workers, scheduler, and callback dispatchers.
It runs `python /app/manage.py migrate_before_start` and only executes the
application command after migrations succeed. A migration failure exits the
container before new web code can serve or new workers can consume tasks.
Container restarts run the gate again. Do not override the image entrypoint
in service definitions.

`migrate_before_start` holds a PostgreSQL session advisory lock while applying
migrations, so containers starting together wait for one migration runner.
The lock is released after success or failure (and PostgreSQL releases it if
the connection dies). Each waiting container checks/applies migrations itself
before starting. SQLite development/test environments run migrations without
the PostgreSQL lock. Schema changes must still support any older containers
that remain running during a rolling deployment.

## Production promotion

The `Deploy Relay Prod (Contabo)` workflow in
[`.github/workflows/deploy-prod.yml`](../.github/workflows/deploy-prod.yml)
is `workflow_dispatch` only (`confirm_production: true`, optional
`image_tag`). It resolves the tag to a dev-proven release — the
`dev-release` artifact of a successful deploy-dev run on main — refuses any
tag no successful DEV run proved, applies
[`deploy/coolify/docker-compose.staging.yml`](../deploy/coolify/docker-compose.staging.yml)
to the prod service through the `production` GitHub environment (required
reviewer), and verifies `https://relay.datatalks.club/health/ready`.
The retired `Deploy Relay Development` SSM/health-poll workflow
(`deploy-development.yml`) was deleted; its health-gate role is absorbed by
deploy-dev.yml. The Coolify workflows verify their own entry point after
each deploy.

### Retired AWS EC2/SSM development path (record)

Until 2026-10-22 the host below served development. It is kept
as a record only: the instance was terminated during the Contabo cutover and
`Deploy Relay Development` no longer uses it.

| Aspect | Value (retired) |
|---|---|
| Region | `eu-west-1` |
| Account | `387546586013` (main) |
| Host | one private `t4g.micro` ARM64 instance in the website VPC, reached through SSM (`i-0a3d2851cb906dc69`, terminated 2026-10-22) |
| Public entry | `https://dev.relay.datatalks.club` behind the dev CloudFront/ALB path on port 8000 |
| Database | shared dev RDS Postgres `dtc_relay_dev` via `RELAY_DATABASE_URL_SECRET_ARN` |
| Queues | one dev-owned pair, `ses-webhooks` and its DLQ; no sandbox queue drains |
| Logs | CloudWatch group `/relay/dev/host` |
| Secrets | Secrets Manager containers only; the deploy never populates values |

The retired deploy assumed the role in the `RELAY_DEPLOYMENT_DEPLOY_ROLE_ARN`
repository variable through the `development` GitHub environment and sent
one SSM command to `RELAY_DEVELOPMENT_INSTANCE_ID` (since removed).

The retired host checked out the exact `GITHUB_SHA` and ran
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
  the request Host. Current `main/dev` exports the key
  (`http://relay-dev.website.internal:8000`), so the website's private calls
  are allowed; if the key is ever absent, only the public hostname stays
  allowed. The value is never wildcarded; host names only;
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

The retired release gate was: `/health/ready` answers, every
required container runs, and a `system.echo` task completes through the
worker. The retired workflow then polled
`https://dev.relay.datatalks.club/health/ready` from outside.

### First-deploy operator follow-up (website client)

The first development deploy only proves the host serves and the worker
drains. The dev website (`https://dev.datatalks.club`) becomes a working
Relay client only after these out-of-band steps — secret values are never
printed, committed, or pasted into chat:

1. The deploy generated `RELAY_BOOTSTRAP_API_KEY` into the host-only
   `/etc/relay/runtime.env` (mode `0600`) and registered it as the
   `development-deployment` key. Copy that value into the dev website's
   `website-dev/integrations` secret (`RELAY_API_KEY` JSON key), following
   the production tenant pattern
   (`scripts/provision_production_tenant.sh`). Alternatively, provision the
   already-shared key into the dev database with
   `provision_client_api_key --organization datatalksclub --client
   dtc-courses --name <name> --raw-key-env <env>`; either way, exactly one
   key value must match on both sides.
2. On the dev host, set the website client row's webhook contract to the
   website's values (Django shell; no management command owns these
   fields):
   `relay_webhook_allowed_origins` must include
   `https://dev.datatalks.club`, and `relay_webhook_signing_secret` must
   equal the website's `RELAY_WEBHOOK_SECRET`.
3. Verify in-VPC before touching the website deploy:
   `GET http://relay-dev.website.internal:8000/api/schedules` with the
   website key answers `200` (any `origin_not_allowed`/`401` here is a
   client-row problem, not a host problem).
4. Only then run the website `Deploy Dev` with an empty
   `confirm_dev_schema_reset` — its `sync_relay_schedules` gate must stay
   enabled and green; never skip it silently.

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
Django checks and the full test suite pass. It finds the EC2 host by its
`Name=relay-sandbox-web` tag, checks that the instance is running and its SSM
agent is Online, then sends it one SSM command. Set the repository variable
`RELAY_SANDBOX_INSTANCE_ID` only to pin a specific instance.

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
instance_id=$(aws ec2 describe-instances \
  --region eu-west-1 \
  --filters Name=tag:Name,Values=relay-sandbox-web Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].InstanceId' --output text)
aws ssm start-session --region eu-west-1 --target "$instance_id"
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
