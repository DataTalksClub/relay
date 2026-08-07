# Requirements

Date: 2026-08-07 (supersedes the 2026-08-06 draft)

## The shape, stated first

One central platform runs background work for the estate. Other services **use**
it over an API; they do not install it. A client asks the platform to do
something, gets an identifier back, and can see the status of that work.

The platform sends email today. It is being generalised to run other work as
well, because email turned out to be one task type rather than the point.

This is the intent, and it is stated first because the previous draft buried it.

## What the previous draft got wrong

Recorded deliberately, because the confusion cost real time and would otherwise
recur.

The 2026-08-06 draft described **two architectures without saying which was
primary**:

- A "Non-negotiable constraints" section describing a *library*: every project
  installs a shared package, runs its own worker, keeps its own queue and
  database. It stated "there is no design in which a shared worker runs another
  project's tasks, and any proposal that implies one is wrong."
- A "Hub topology" section describing a *service*: "the hub owns render, send
  and track", "the hub exposes one generic job resource", "the hub is
  authoritative", and — at R5 — one project "becoming the hub the other two
  send through".

Both are in the same document. Work proceeded on the library reading, which
produced per-project task tables and no central place, which is the opposite of
the goal.

The apparent contradiction dissolves once the question is asked precisely:

> **Whose code does the worker import?**

A worker cannot import a *client's* task code — that would need the client's
models, settings and database. That is a genuine constraint and it is what the
old constraints section was reaching for.

But the platform does not run client code. **It runs its own.** A client
requests a task type the platform already implements. Nothing foreign is ever
imported, and the work is centralised. Both halves were right; they were about
different code.

Write it this way from now on: *the platform owns the code it runs.*

## Constraints

**The platform owns every task implementation it executes.** A client names a
task type and supplies parameters. A client never ships code to the platform.
This is what makes one runtime possible.

**Domain decisions stay with the domain data.** Deciding *who* should receive
something needs enrolments, deadlines and tiers, which live in the calling
project. Only the doing moves. A client that needs to compute a recipient list
on a schedule keeps that job locally.

**So clients may still have local background work,** and the console must show
it alongside platform work. The shared package remains available for that. It
is no longer the primary architecture — it is what a client uses for the
residue that cannot move.

**Email is unrecallable.** It is the only output in the estate that cannot be
withdrawn, which constrains cutovers everywhere below.

## Context

Three Django projects run background work, with five execution models across
three compute platforms and three unrelated status surfaces:

- **ai-shipping-labs** — django-q2 on the ORM broker, ECS Fargate. ~90% of the
  estate's background work: 24 cron schedules, ~40 enqueue sites across 12
  apps. Worker shape differs between production and other environments, chosen
  by branches in a deploy script rather than configuration.
- **course-management-platform** — no queue. A hand-rolled database outbox
  drained by a scheduled ECS task every five minutes, plus two scheduled
  commands. Already a client of the platform for email.
- **datamailer** — the platform in embryo. Per-client API keys, tenant scoping,
  a task queue, and a status contract. Runs on one EC2 host with Postgres.
  Sandbox only; carries CMP production email despite that.

The stated driver is manageability, not cost: one mental model instead of
three, and one place a failure can be found. Cost was assessed as a weak
motivation — the always-on worker footprint is already near zero.

## Functional requirements

### R1 — One interface

All background work is defined and enqueued through `django.tasks`. No project
imports a queue backend directly; the backend is swappable by configuration.

### R1a — Ingress from cloud services

Not all work originates in application code. Provider notifications and inbound
mail are delivered by cloud services straight into queues, so a queue only
Django can write to is insufficient.

A thin ingress path stays: a queue the provider writes to, drained by a worker
that hands each message to the task system rather than processing it inline.
Processing inline puts that work on a second execution path where it is
invisible to the console — which is the blind spot this exists to remove.

### R2 — One compute shape

Workers run the same way everywhere: alongside the web process, on the same
platform, by the same mechanism, in every environment. Parity is a property of
configuration, not of which branch of a deploy script an environment takes.

**A worker must be part of the deployment definition, not a manual step.** A
project that enqueues work with no worker running accepts tasks and silently
never runs them. The deploy must fail when the worker is absent rather than
succeed quietly.

### R3 — Status that crosses service boundaries

One logical operation spans services:

```
client "send campaign 42" → platform job → N child tasks → provider → events → callback
```

A correlation identifier minted by the originator survives the API call, the
execution on the platform, and the callback. "Did it go out" is answerable from
the originating project regardless of where the work ran.

First-migration requirement. Threading an identifier retroactively means
backfilling across two services.

### R4 — Progress, modelled by workload shape

Three shapes, handled differently on purpose.

**Countable fan-out.** The parent knows the total before enqueuing children.
Common: campaign sends chunk a recipient list, notification fan-outs iterate
registrations. The parent declares the total; the numerator is derived from
finished children, so a child that dies without reporting cannot corrupt it.

**Opaque single unit.** Most scheduled jobs and every individual send. No
meaningful numerator. These need elapsed time against a rolling baseline of
previous runs of the same task, so "is it stuck" is answerable.

**Deferred external outcome.** Opens and clicks arrive over days and never reach
a terminal state. Not progress. Modelling them as progress makes every campaign
appear to run for a week. They belong on the domain object.

### R5 — Tenant scoping

The platform is multi-client. A client sees only its own jobs. The owner field
exists from the first migration; adding it later means backfill plus auditing
every existing query for the missing filter.

### R6 — Per-task credentials

The platform's own role must not carry the union of every permission any task
needs — that concentration gets worse, not better, once one service runs work
for the whole estate.

Each task type has a role scoped to exactly what it does. The platform's role
is an almost-powerless identity whose only permission is assuming those roles.

- Default-deny: a task with no mapping gets a session with no permissions,
  never the parent role.
- The role session name carries the task run identifier, so audit entries join
  back to the task using the same identifier as R3.
- Credentials refresh: long fan-out parents outlive a default session lifetime,
  and mid-task expiry presents as a random permissions failure.
- No module-level cloud clients: anything built at import time picks up ambient
  credentials and bypasses the scheme.

This buys scoping per unit of work, audit attribution, and blast-radius
reduction. It is **not** a tenant boundary and must not be described as one — a
compromised worker can assume any of the roles.

Where per-tenant sending identities are involved, the role lives in the
tenant's own infrastructure and the platform assumes into it, so a routing bug
produces an access denial rather than mail correctly delivered from the wrong
domain.

### R7 — One console

A single page showing, for the platform and for every project with local work:
worker liveness, queue depth and oldest pending age, schedules with last and
next run, recent runs with status and progress, and a failure count.

Reads a versioned JSON contract over HTTP. Holds no state beyond a cache and has
no database.

Pull, not push. A console that polls learns a project is dead because the fetch
fails; one that waits for pushes cannot tell a healthy quiet project from a
dead one.

Deploys independently of what it watches — embedding it in the largest project
means the tool used to diagnose an outage dies with the thing most likely to be
having one.

Paging stays on cloud-provider alarms, which survive the console being down.

**This is the piece that delivers "one central place" for observation, and it
does not exist yet.** Its absence is why the estate feels decentralised even
where the architecture is right.

### R8 — Transactional enqueue

Enqueuing participates in the transaction that caused it, so a committed change
and its follow-up work cannot diverge. This removes the hand-rolled outbox and
the per-call-site hooks, and closes the class of bug rather than relying on
every author remembering.

### R9 — Serialisation by lock, not by global concurrency cap

Rate-limited providers need work serialised per key — per client, per
configuration set — not globally. A global cap of one serialises unrelated
clients against each other and does not express the real constraint.

### R10 — A task's result must survive the backend

Whatever a task returns is stored by the queue backend. A task that returns a
model instance or another non-serialisable object fails *after* its work has
committed — the email is sent and the record says it failed.

Tasks return plain data. This is a requirement rather than a convention because
the failure is invisible in tests that never exercise the real backend.

## What is shared, and how

**The platform's internals** — backend wiring, the task run model, enqueue and
progress helpers, credential resolution, the status collector — live in one
package. Three separate implementations would diverge on what counts as failed
and whether a correlation identifier survives a retry. If "failed" means
something different per project, the console does not aggregate; it lies.

**The client-facing surface is an API, not a package.** A client integrates by
calling the platform and receiving callbacks. Only a client with residual local
work installs the package.

**The read path is a contract.** The status endpoint, its authentication, and
per-project entity resolution differ by nature and stay local.

Compatibility: package migrations stay additive for several releases. The
contract carries a version the console tolerates older values of, so each
project upgrades on its own schedule — which matters once the platform's
release cadence diverges from its clients'.

## Platform topology

The client owns who gets what and when. The platform owns doing it and
reporting on it.

The platform exposes one generic job resource rather than per-feature status
endpoints, and the job payload uses the same shape as the internal status
contract, so the console renders a platform job with the code it already uses
for a local task.

A client-supplied idempotency key is mandatory: a client retrying after a
timeout must not cause a double send.

### Status ownership

The platform is authoritative. Clients hold a projection that is explicitly a
cache, never a second source of truth. A split where both sides own status and
history produces reconciliation bugs.

Three parts, because callbacks alone are insufficient:

1. Callbacks maintain the projection, so a client page renders locally and
   survives the platform being slow.
2. A reconciliation sweep re-pulls any non-terminal chain after a bounded
   interval, because callbacks get lost.
3. An explicit refresh on the detail page, for when someone is watching.

## Migration stance

Migration cost is not a design constraint. Where the from-scratch answer differs
from evolving what exists, the from-scratch answer wins and the migration is
work to be done.

Two things this does not license:

**Rewriting working application code.** Infrastructure is disposable; tested
domain behaviour is not.

**Big-bang cutover of anything irreversible.** Email is the only unrecallable
output in this estate; a bad cutover sends the whole list duplicates or nothing.
It also stays clear of any platform migration, because two simultaneous changes
make a breakage undiagnosable.

## Open decisions

Unresolved. Listed so plans do not assume an answer.

1. **What "ad-hoc task" means.** Either the platform implements a catalogue of
   task types that clients invoke by name — the natural extension of how email
   templates work today — or clients supply code to run, which means sandboxing,
   resource limits and code distribution, and brings back the foreign-code
   problem as an isolation problem. These differ by an order of magnitude in
   scope. Recommendation: the catalogue.
2. **The platform's name.** It sends email and runs general work, so a name
   leaning either way will age badly. Recommendation: *Relay* — a mail relay is
   literally the email half, and relaying work onward is the general half.
   Alternatives considered: Dispatch, Depot, Courier. `taskdeck` is better kept
   for the package than promoted to the platform.
3. Whether a maintained `django.tasks` backend adapter exists for the chosen
   queue, or whether it is ours to write.
4. Real send volume at the platform, and the resulting connection count against
   a small database instance.
5. Whether the existing container size fits a web process plus a worker for the
   two projects that do not run one today.
