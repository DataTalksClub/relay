# Moving To A Managed Database

Datamailer runs Postgres as a container on the same EC2 host as the app
(`db` in `deploy/docker-compose.yml`). That is deliberate for a sandbox: it
costs nothing beyond the instance already being paid for, and it is one less
thing to provision. This document is the exit path, written while the reasons
are fresh rather than reconstructed later under pressure.

## What makes the switch cheap

The app never names a database. `datamailer/settings.py` builds `DATABASES`
from `DATABASE_URL` through `dj_database_url`, and `deploy/docker-compose.yml`
defaults that variable to the local `db` service rather than hardcoding it:

```yaml
DATABASE_URL: ${DATABASE_URL:-postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}}
```

Setting `DATABASE_URL` explicitly in `.env` overrides that default for every
service at once -- `web`, `worker`, `migrate`, and both ingress drains. No
application code changes. Keep it that way: anything that reads
`POSTGRES_HOST` or similar directly re-couples the app to this host.

## Triggers

Move when one of these is true, not on a schedule:

- The host is no longer disposable, i.e. losing it would lose data that
  matters. Container Postgres shares the instance's fate.
- Backups need to be someone else's job. There is no automated backup today;
  a snapshot of the EBS volume is the whole story.
- A second app host is needed. Two hosts cannot share a container database,
  and the `SES_MAX_SEND_RATE` process-local lock has the same constraint.
- Postgres CPU or memory starts competing with gunicorn and the worker on the
  same instance.

## Cost, as of 2026-08 (us-east-1, on-demand, verified against the Pricing API)

| | Monthly |
|---|---|
| Container Postgres on the existing host | $0 |
| RDS `db.t4g.micro`, Single-AZ | $11.68 + storage |
| RDS `db.t4g.small`, Single-AZ | $23.36 + storage |
| gp3 storage | $0.08/GB |

Multi-AZ roughly doubles the instance line. For reference the whole app host
(`t4g.small`) is $12.26/month, so the smallest sensible RDS instance about
doubles the bill. That is the trade being made: it buys managed backups,
point-in-time recovery, and a database that outlives the instance.

Aurora Serverless v2 is the other option and scales to zero on v2's newer
minimum, but its floor is above `db.t4g.micro` for a workload this small.

## Procedure

1. **Provision.** RDS Postgres in the same VPC, private subnet, encrypted, not
   publicly accessible. Security group allows 5432 only from the app host's
   security group. Match the major version to the `postgres:17` image so the
   dump restores cleanly.

2. **Dump.** From the app host, with the stack still running:

   ```bash
   cd /opt/datamailer
   docker compose -f deploy/docker-compose.yml --env-file .env exec -T db \
     pg_dump -U datamailer_user -Fc datamailer > /tmp/datamailer.dump
   ```

3. **Quiesce.** Stop the writers so the dump is not already stale. `web` can
   keep serving reads for a moment, but the worker and drains must stop:

   ```bash
   docker compose -f deploy/docker-compose.yml --env-file .env stop worker ingress-ses ingress-inbound
   ```

   Re-dump after stopping. The first dump was only to size the outage.

4. **Restore.**

   ```bash
   pg_restore -h <rds-endpoint> -U datamailer_user -d datamailer --no-owner /tmp/datamailer.dump
   ```

5. **Switch.** In `/opt/datamailer/.env`, set `DATABASE_URL` to the RDS
   endpoint. Then `up -d`; the `migrate` service runs against the new database
   and should report no pending migrations.

6. **Verify** before deleting anything:
   - `/health/` returns 200.
   - `/internal/ops/status` shows the expected task counts, not zeroes. Zeroes
     usually mean an empty database rather than an idle one.
   - Row counts match the source for `contacts`, `transactional_messages`, and
     `email_events`.
   - Enqueue one task and confirm the worker moves it to `success`.

7. **Retire.** Remove the `db` service and the `postgres_data` volume from
   `deploy/docker-compose.yml`, and drop `POSTGRES_PASSWORD` from `.env`.
   Keep the EBS snapshot from step 2 until the new database has a backup
   history of its own.

## Rollback

Until step 7, rollback is putting the old `DATABASE_URL` back and running
`up -d`. The container database is still there with its data. After step 7 it
is not, which is why step 7 is last and separate.

## Sequencing note

Do this while the queue is empty. `django-tasks-db` keeps task rows in the same
database, so a mid-flight task is a row that exists in the source and not the
target. Drain the worker first -- `/internal/ops/status` reporting no running
or queued tasks is the signal.
