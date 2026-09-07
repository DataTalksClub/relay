# Relay — Context For A Fresh Start

Date: 2026-08-07

Read this first. It is written for someone with no prior conversation history.
It records what Relay is for, in the owner's own framing, plus the state of the
estate around it and the mistakes already paid for.

Companion documents:

- [requirements.md](requirements.md) — what Relay must do
- [unification-plan.md](unification-plan.md) — the ordered work to get there

## What the owner asked for, in their framing

Collected across a working session on 2026-08-07. Paraphrased closely, because
the precise wording matters — an earlier draft lost the intent by re-phrasing it
into something more conventional.

> "I want to have one central place for the tasks. The way I see it is, a
> service that uses this requests a task and can see the status of the task
> being performed. It doesn't *include* it — it *uses* it."

> "Ad-hoc means tasks that are not related to sending emails but they still
> require a worker. I don't want to maintain a separate worker for all my Django
> projects. I have a lot of Django projects, all require a worker — that's an
> instance I have to deploy separately. I don't want that. I want to centralize
> it in one place, so if there are jobs they need to do, they run through the
> centralized task queue."

> "When a task starts it can assume a role, so it has access to the things it
> needs."

> "Since datamailer is the main user and we must still use it for sending
> mails, it makes sense to unify them."

> "Create the infra for this Relay separately, because there are some services
> that still use datamailer and we don't want to break it."

> "There will be another client — the website I'm designing for DataTalksClub.
> We'll use it for sending emails and other background tasks."

> "I don't want to pay too much."

Decisions taken from this:

| Decision | Value |
|---|---|
| Name | **Relay** — a mail relay is the email half; relaying work onward is the general half |
| Shape | A service clients **use** over an API, not a library they install |
| Ad-hoc tasks | Central worker, work reachable without importing client code |
| Datamailer | Keeps running untouched; Relay is parallel |
| Infrastructure | New, separate Terraform root; no shared resources |
| Cost | Kept low; no new database instance if avoidable |

## The problem to keep in front of you

**A worker runs a task by importing it.** Importing `aisl.tasks.build_report`
needs AISL's models, settings and database. One central worker cannot hold every
project's code and credentials.

That is why an earlier design concluded "a worker per project" and built a
shared *library*. The owner's goal is the opposite, and the library shape does
not meet it.

The resolution is that **the unit of work need not be a Python import.** Relay's
worker can make an authenticated HTTP call to the client's existing web process.
Relay owns the queue, retries, schedule, concurrency and status; the client owns
the code and deploys no worker. See "Three kinds of task" in
[requirements.md](requirements.md).

Get this right and the rest follows. Get it wrong and you rebuild the library.

## The estate

### AWS accounts

| Account | ID | Holds |
|---|---|---|
| main | `387546586013` | CMP, AI Shipping Labs, shared infra. Region `eu-west-1` |
| sandbox | `817685572750` | datamailer and experiments. Region `us-east-1` for legacy reasons |

Promotion rule from `aws-infra/docs/state-boundaries.md`: workloads moving out
of sandbox go to `eu-west-1` unless a service forces otherwise. Do not carry
`us-east-1` forward into Relay by copying datamailer's root.

### Repositories

| Repo | Purpose |
|---|---|
| `DataTalksClub/relay` | this one — the unified platform |
| `DataTalksClub/datamailer` | the live email service; **still in production use** |
| `DataTalksClub/taskdeck` | the task layer; its history is merged in here under `taskdeck-src/` |
| `DataTalksClub/aws-infra` | all Terraform, one root per workload |
| `DataTalksClub/course-management-platform` | CMP — an existing client |

### What is deployed right now

**datamailer**, instance `i-044716a7d6b053eef` (t4g.micro), sandbox account,
`us-east-1`, serving `https://datamailer.dtcdev.click`.

- Postgres 16 on the same host (migrated from SQLite on 2026-08-07). Not RDS —
  it costs nothing extra and the dataset is ~113 MB.
- 2 GB swap file; the box is 906 MB and was OOM-tight before it.
- systemd units: `datamailer` (gunicorn), `datamailer-db-worker`,
  `datamailer-ses-webhooks-worker`, `datamailer-inbound-email-worker`,
  `datamailer-cmp-callbacks-worker`,
  `datamailer-recipient-list-imports-worker`.
- Deploys automatically from `main` via `.github/workflows/deploy-sandbox.yml`,
  over SSM. **Pushing to datamailer's main deploys to production.**
- Real data: ~3,460 contacts, ~12,600 transactional messages, ~38,700 email
  events, 3 clients.

**CMP production depends on it.** `main/cmp/app_prod.tf` sets
`DATAMAILER_URL = "https://datamailer.dtcdev.click"`. Prod and dev CMP both
point at this one sandbox host. Breaking it stops course email.

### Queues

Live SQS queues in the sandbox account. Only two still have producers:

| Queue | Producer | Status |
|---|---|---|
| `datamailer-sandbox-ses-webhooks` | SNS, from SES | **active**, ~2,800/week |
| `datamailer-sandbox-inbound-email` | S3 object events | **active**, low volume |
| `datamailer-sandbox-transactional-email` | none since 2026-08-07 | retired |
| `datamailer-sandbox-campaign-email` | none | retired |
| `datamailer-sandbox-email-events` | none, never used | retired |

The first two are fed by AWS itself, so they cannot move onto an in-application
queue. Relay needs the same ingress shape.

## This repository

Both codebases are here with full history — 200 commits, merged rather than
copied, so the reasoning behind each is still reachable via `git log`.

```
relay/
  datamailer/      Django project (settings, wsgi, urls) — to be renamed `relay`
  mailing/         the email domain app — keep the name and every db_table
  taskdeck-src/    the taskdeck repo as merged; to be unpicked into place
  cli/  docs/  infra/  scripts/  tests/  templates/  static/
```

`taskdeck-src/` is deliberately parked. Unpicking it is the first task in the
unification plan.

## Mistakes already paid for

All four reached production in datamailer on 2026-08-07. They are cheap to
repeat and expensive to notice.

**1. A worker that was never deployed.** Internal queues moved onto
`django.tasks`, but the deploy script only installed the old SQS worker units.
Tasks were written to the database and nothing executed them. Everything looked
healthy. Assert the worker is active in the deploy so its absence fails the
deploy.

**2. Tasks returning model instances.** The backend stores return values as
JSON. A task returning a `TransactionalMessage` raised *after* the send
committed — the email went out and the record said `failed`. Tasks return plain
data. No unit test on either side caught it, because the one task exercised end
to end happened to return `None`.

**3. Ingress processed inline.** SES notifications were handled directly in the
SQS worker rather than handed to the task system, so ~2,800 events a week ran on
a second execution path invisible to the status contract. Ingress must enqueue,
not process.

**4. SQLite with many writers.** Nine processes on one file produced
`database is locked` 500s on bulk sends under real load. Fixed with WAL, a busy
timeout and `IMMEDIATE` transactions, then properly by moving to Postgres. Do
not start Relay on SQLite.

## Working agreements

- **`uv`, not `pip`.** `uv add`, `uv run python manage.py …`.
- **Never break datamailer.** It is in production use despite being called a
  sandbox. Relay shares nothing with it.
- **Infrastructure changes go to `aws-infra` as a pull request**, not applied
  directly.
- **Email is unrecallable.** No big-bang cutover of a send path.
- `AGENTS.md` at the repo root carries the inherited conventions; note that its
  claim that datamailer has no production deployment is now misleading — CMP
  production depends on it.

## Where to start

1. Read [requirements.md](requirements.md), especially "Three kinds of task".
2. Follow [unification-plan.md](unification-plan.md) in order. Phase 1 is
   mechanical and should stay green throughout; do not begin Phase 3 until the
   suite passes.
3. Raise the infrastructure as a PR against `aws-infra` — do not apply it.

## Open decisions — do not invent answers

1. **Webhook task timeout ceiling.** Decided in R1.2 (relay issue #6): 60
   seconds synchronous, with an ack-then-callback mode for longer work — the
   receiver answers `202` with `{"lease_seconds": N}` and later completes or
   fails the task through the API; an expired lease fails the task without
   re-execution. See "Timeout ceiling and the 202 lease protocol" in
   [api.md](api.md). The remaining decisions below are still open.
2. **Compute shape for Relay.** Datamailer's single EC2 host with systemd is
   what it grew, not what was chosen. Decide fresh.
3. **Whether AISL migrates**, and when. Largest win, largest migration. Not
   required for a first release.
4. **What the DataTalksClub website needs first.** It is the first client to use
   both halves, so the generic job API should be designed against its real
   requirements rather than guessed.
