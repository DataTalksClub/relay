# Relay production

Relay production is not deployed. The Terraform root exists and this
repository has the deploy path, but no release has run on the production
host yet, and no client sends through it. Do not describe production as
live until the first deploy has been made and verified by the owner.

## Environment

The infrastructure lives in `main/relay` of `DataTalksClub/aws-infra` and is
applied by the owner, never from this repository.

| Aspect | Value |
|---|---|
| Region | `eu-west-1`, including SES |
| Account | `387546586013` |
| Host | one private `t4g.small` EC2 instance in the website VPC, no public address, reached through SSM Session Manager |
| Public entry | `https://relay.datatalks.club`, monitoring and admin UI only, behind the shared ALB through a CloudFront distribution with single sign-on and a WAF rate limit |
| Website path | the website calls Relay in-VPC at `http://relay.<internal zone>:8000` |
| Database | dedicated RDS Postgres `relay-production` (`db.t4g.medium`), deletion protection, 7-day automated backups |
| SES | Relay-owned configuration set `relay-production`; sending domain identity `datatalks.club`; default From `relay@datatalks.club` |
| Queues | one pair, `ses-webhooks` and its DLQ; Relay production is outbound only |
| Logs | CloudWatch group `/relay/production/host`, 30-day retention |
| Secrets | Secrets Manager value containers `database-url`, `django-secret-key`, `api-keys`; Terraform creates containers only, values are populated out of band |

## How a deploy works

Dispatch the `Deploy Relay Production` workflow in this repository. It never
runs on push.

1. The `test` job runs lint, Django checks, and the full test suite.
2. The `deploy` job requires the `production` GitHub environment, which must
   be configured with a required reviewer, so an owner approves every deploy.
3. It assumes the `relay-production-github-deploy` IAM role and sends one SSM
   command to the host from `main`.
4. The host checks out the commit and runs
   `scripts/deploy_relay_sandbox.sh --environment production <commit>`.
5. The script fetches `SECRET_KEY` and `DATABASE_URL` from Secrets Manager,
   migrates, provisions the production tenant, starts the containers, and
   fails unless `/health/ready` answers, every required container runs, and a
   `system.echo` task completes through the worker.

Repository variables the owner sets once:

- `RELAY_PRODUCTION_INSTANCE_ID`: the instance id from the `main/relay` `host` output.
- `RELAY_PRODUCTION_DEPLOY_ROLE_ARN`: optional; it defaults to the role ARN from the same root.

Differences from the sandbox deploy path:

- No PostgreSQL container and no Caddy: the database is RDS, and the shared
  ALB terminates HTTPS, so the web container binds `0.0.0.0:8000`.
- No inbound mail ingress: production has no SES receipt rule, no inbound
  queue, and no `SQS_INBOUND_EMAIL_QUEUE_URL`.
- Every container carries an explicit `--memory` limit because the host has
  2 GiB and no PostgreSQL. The limits are a first sizing and can be tuned
  from the host's memory alarm.
- `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `DEFAULT_FROM_EMAIL`,
  `AWS_SES_REGION`, and `AWS_SES_CONFIGURATION_SET` come from
  `/etc/relay/infrastructure.env` and are never rewritten by the deploy.
- `RELAY_SES_MAX_SEND_RATE` is not set, so the application default of 10
  messages per second applies.
- There is no public health URL to poll after the deploy; the release gate is
  the local readiness check, the container checks, and the in-host smoke test.

## Provisioning the production tenant

`scripts/provision_production_tenant.sh` runs on the production host, after
at least one deploy has built the release image. It is idempotent and is also
invoked by every production deploy, so tenant state converges.

- It provisions organisation `datatalksclub`, audience `dtc`, and client
  `dtc-website`.
- It configures the sender `hello=DataTalks.Club <hello@datatalks.club>` as
  the client default. The final sender address is to be confirmed by the
  owner before the first real send.
- It generates one client API key into `/etc/relay/runtime.env` (mode `0600`)
  following the sandbox `RELAY_BOOTSTRAP_API_KEY` mechanism, registers it as
  `production-deployment`, and never prints key material. The owner copies
  the key from that file into the DTC website deployment secrets.

Preview without changes:

```bash
bash /opt/relay/scripts/provision_production_tenant.sh --dry-run
```

## Deviations from the plan text

The plan issue describes a production root with an Elastic IP, an
Relay-owned SES identity, and the full set of AWS-fed ingress queues. The
merged `main/relay` root deviates deliberately:

- No Elastic IP: the host is private behind the shared ALB, so a stop/start
  cannot break DNS because no DNS record points at the host address.
- One SES-webhook ingress queue instead of two: inbound mail is out of scope
  for Relay production, and no receipt rule, bucket, or inbound queue exists
  in this root.
- The `datatalks.club` SES identity, DKIM, SPF, and DMARC records are owned
  by `main/common` and `main/website-static`; `main/relay` only grants scoped
  send access and creates its own configuration set `relay-production`.

These conflicts with the plan text are reported to the plan owner as a plan
fix. Nothing in this repository implements or resolves them.

## First deploy checklist (owner)

- Populate the Secrets Manager containers `relay-production/database-url`
  (from the RDS master credential) and `relay-production/django-secret-key`.
- Confirm the final sender address for the `dtc-website` client.
- Verify the `datatalks.club` identity is verified in the SES console and
  that the account is out of the SES sandbox.
- Set `RELAY_PRODUCTION_INSTANCE_ID` and configure the `production` GitHub
  environment with a required reviewer.
- Dispatch the workflow from `main` and approve it.
- Install `/usr/local/sbin/relay-prune-database` once a retention command
  exists in the application; the EventBridge schedule already invokes it
  daily at 03:30 UTC, and delivery history grows tens of GB per year without
  it.

## On-call checks

- Monitoring UI: `https://relay.datatalks.club` answers through single
  sign-on.
- Host: `aws ssm start-session --region eu-west-1 --target <instance id>`,
  then `docker ps`, `git -C /opt/relay rev-parse HEAD`, and
  `curl -fsS http://127.0.0.1:8000/health/ready`.
- Worker: submit a `system.echo` task on the host with
  `scripts/smoke_test_relay.py --base-url http://127.0.0.1:8000`; a broken
  worker fails it.
- Ingest: the `ses-webhooks` queue age alarm stays quiet and the DLQ stays
  empty after real sends.
- Alarms: queue age, DLQ depth, host memory/disk/CPU credits, database
  storage/CPU/availability, and SES bounce and complaint rates publish to the
  operator SNS topics configured in `alarm_action_arns`.
- Logs: `docker logs --since 15m relay-web`, `relay-worker`,
  `relay-scheduler`, `relay-ses-ingress` on the host, or the
  `/relay/production/host` log group.

## Roll back

- Pick a known-good commit, then through SSM:
  `git -C /opt/relay fetch origin <commit> && git -C /opt/relay checkout --force <commit> && bash /opt/relay/scripts/deploy_relay_sandbox.sh --environment production <commit>`.
- Do not delete `/var/lib/relay` during a rollback; it carries the send spool
  and application state.
- The database is RDS: point-in-time restore and the final-snapshot policy
  are owner decisions. Pause sends first if delivery is affected.
- Schema drift: if a rollback must cross a migration that cannot be undone,
  restore the database instead of reverting the migration.

## Not run here

The plan issue's live checks are owner-gated and were not executed for this
change: `Not run here, needs: owner approved and applied the Terraform root
and the first production deploy`.

- Canary email through the production path.
- The `datatalks.club` SES identity verified in the AWS console.
- `system.echo` against the production deployment.
- `GET /internal/ops/status` from the production host.
