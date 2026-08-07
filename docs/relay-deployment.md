# Relay sandbox deployment

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
