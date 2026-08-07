# Relay — Requirements

Date: 2026-08-07
Status: specification. Not yet implemented.

## What Relay is

One service that runs background work for the estate. Other projects **use** it
over an API; they do not install it and they do not run their own workers.

It does two things:

1. **Sends email.** This is what datamailer does today, and it continues
   unchanged for existing clients.
2. **Runs everything else.** Any Django project that needs background work asks
   Relay instead of deploying a worker of its own.

The second is the reason Relay exists. There are several Django projects in the
estate; each currently needs its own worker process deployed, monitored and
paid for. One central worker replaces all of them.

## The problem that shapes everything

**A worker runs a task by importing it.** Importing `aisl.tasks.build_report`
requires AISL's models, settings and database connection. A central worker
cannot hold every project's code and credentials, and any design that assumes it
can is wrong.

That constraint is real. It does not, however, force a worker per project —
because the unit of work does not have to be a Python import.

## Three kinds of task

Relay must support all three. They differ in *where the work runs*, which is
what determines whether a client needs a worker.

### 1. Built-in tasks

Relay implements the work itself. The client names a task type and passes
parameters.

```
POST /api/tasks
{ "type": "email.send", "idempotency_key": "...", "params": { ... } }
```

Sending email is one of these. So is anything Relay can do without reaching
into a client's database — render a document, call a third-party API, process
an upload.

Nothing foreign is imported. This is the catalogue model, and it is how
datamailer already works for email.

### 2. Webhook tasks — the one that removes per-project workers

The client registers work as **an HTTP call Relay will make on its behalf**:

```
POST /api/tasks
{ "type": "webhook",
  "url": "https://aisl.example/internal/jobs/rebuild-index",
  "payload": { ... },
  "idempotency_key": "..." }
```

Relay's central worker queues it, calls the URL, retries with backoff on
failure, enforces a timeout, and records the outcome. **The work itself runs in
the client's existing web process** — which is already deployed. The client
adds an endpoint, not a worker.

This is the mechanism that delivers "one central worker for all my Django
projects". Relay owns queueing, retries, scheduling, concurrency and status;
the client owns the code, and needs no new deployment.

What the implementer must get right:

- **The client endpoint must be authenticated.** Relay signs the request (HMAC
  over body plus timestamp) so the client can verify it came from Relay and is
  not a replay.
- **The endpoint must be idempotent.** Retries are guaranteed to happen. Relay
  passes its task id; the client uses it to deduplicate.
- **Timeouts bound the work.** An HTTP request is not a good home for a
  twenty-minute job. Document a limit and make it explicit in the API. For work
  that exceeds it, the client's endpoint should acknowledge quickly and report
  completion back to Relay — see the callback path in R3.
- **The work competes with request handling.** The client should route these to
  a path it is willing to have occupied. Say so in the client documentation.

### 3. Schedules

A cron expression owned by Relay, firing either of the above. This replaces
per-project EventBridge rules and django-q2 schedules, so a project that only
needed a worker for cron needs nothing at all.

Schedules must be visible in the status contract with last run, last success,
and whether a run was missed. A schedule that silently stops firing is
indistinguishable from a quiet period otherwise.

## Requirements

### R1 — One interface internally

Relay's own tasks are defined and enqueued through `django.tasks`. No direct
import of a queue backend; the backend is a settings change.

### R2 — The worker is part of the deployment

A deployment that brings up the web process without a worker must fail, not
succeed quietly. Enqueuing with no worker running accepts work and never
performs it, which is invisible until someone asks why nothing arrived.

This happened in datamailer on 2026-08-07 and went unnoticed until a queue was
inspected by hand.

### R3 — Status that crosses the boundary

A correlation identifier minted by the client survives the API call, the
execution in Relay, and the callback. "Did it run" is answerable from the
calling project.

Relay is authoritative for status. Clients may cache a projection; it is never
a second source of truth. Callbacks maintain the projection, a reconciliation
sweep re-pulls non-terminal work because callbacks get lost, and a manual
refresh exists for when someone is watching.

### R4 — Progress by workload shape

- **Countable fan-out:** parent declares the total before enqueuing children;
  the numerator is derived from finished children so a child that dies without
  reporting cannot corrupt it.
- **Opaque single unit:** no numerator. Elapsed time against a rolling baseline
  of previous runs of the same task, so "is it stuck" is answerable.
- **Deferred external outcome** (email opens, clicks): not progress. Modelling
  them as progress makes work appear to run for days. They belong on the domain
  object.

### R5 — Tenant scoping

Relay is multi-client. A client sees only its own work. Every task carries an
owner from the first migration — adding it later means backfill plus auditing
every query for the missing filter.

Client authentication is a per-client API key, as datamailer does today.

### R6 — Per-task credentials

Relay's own role must not carry the union of every permission any task needs.
That concentration gets worse once one service runs work for the whole estate.

Each task type has a role scoped to what it does; Relay's role can only assume
them. Default-deny: a task with no mapping gets no permissions, never the
parent role. The session name carries the task run id so audit entries join back
to the task. Credentials refresh, because long fan-out parents outlive a default
session lifetime.

Not a tenant boundary — a compromised worker can assume any role. Do not
describe it as one.

### R7 — Idempotency is mandatory

Every task submission carries a client-supplied idempotency key. A client
retrying after a timeout must not cause a second send or a second job.

### R8 — Transactional enqueue

Enqueuing participates in the transaction that caused it, so a committed change
and its follow-up work cannot diverge.

### R9 — Serialisation by lock, not a global cap

Rate-limited providers need work serialised per key — per client, per
configuration set — not globally. A global concurrency cap of one serialises
unrelated clients against each other.

### R10 — Task results must survive the backend

The queue backend stores return values as JSON. A task returning a model
instance fails *after* its work has committed — the email is sent and the record
says it failed. Tasks return plain data.

This is a requirement, not a convention, because it is invisible to any test
that does not exercise the real backend. It reached production in datamailer.

### R11 — One console

One page showing, for Relay and any project with residual local work: worker
liveness, queue depth and oldest pending age, schedules with last and next run,
recent runs with status and progress, failure count.

Reads the versioned JSON contract over HTTP. No database. Pull, not push — a
console that polls learns a project is dead because the fetch fails. Deploys
independently of what it watches.

Not built yet. It is the piece that delivers "one central place" to look.

## Constraints

**Relay owns every task implementation it executes.** Clients name a task type
or supply a URL. Clients never ship code to Relay.

**Domain decisions stay with the domain data.** Deciding *who* should receive
something needs enrolments and deadlines, which live in the calling project.
Only the doing moves.

**Datamailer must keep working throughout.** It carries CMP production email.
Relay is a parallel deployment with its own database, queues and domain. No
shared state, no shared infrastructure, no cutover until Relay is proven.

**Email is unrecallable.** It is the only output in the estate that cannot be
withdrawn. No big-bang cutover of a send path.

## Clients

| Client | Uses Relay for | Status |
|---|---|---|
| course-management-platform | transactional email, campaigns | live today against datamailer |
| DataTalksClub website | email, and general background work | new; being designed now |
| ai-shipping-labs | general background work | candidate; has ~24 schedules and ~40 enqueue sites |

The website is the first client that will use both halves, so it is the one to
design the generic job API against.

## Open decisions

1. **Webhook task timeout ceiling.** Bounds how much work fits the model, and
   determines whether an ack-then-callback path is needed in the first version.
2. **Whether AISL migrates.** It carries most of the estate's background work
   and is the largest single win, but also the largest migration. Not a
   first-release requirement.
3. **Compute shape for Relay itself.** Datamailer runs on one EC2 host with
   systemd. Relay should be decided fresh rather than inheriting that.
