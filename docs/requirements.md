# Requirements

Date: 2026-08-06

## Context

Three Django 6.0 projects run background work today, each with a different
mechanism, and two of them on different compute platforms:

- [ai-shipping-labs](https://github.com/) — django-q2 with the ORM broker, a
  `qcluster` sidecar in a combined ECS Fargate task. Carries roughly 90% of the
  background work: 24 cron schedules and ~40 event-driven enqueue sites across
  12 apps.
- [course-management-platform](https://github.com/DataTalksClub/course-management-platform)
  — no queue. A hand-rolled database outbox drained by a scheduled ECS task
  every five minutes, plus two other scheduled commands.
- [datamailer](https://github.com/DataTalksClub/datamailer) — SQS with Lambda
  workers via event source mappings in production, and the same handlers as
  systemd units in the sandbox environment.

Five execution models, three compute platforms, three unrelated status
surfaces. The goal is one of each.

The two motivations, in the order they were given: cost, and being able to
manage the estate without holding three different mental models.

Cost turned out to be a weak motivation on inspection — the always-on worker
footprint is already close to zero, because AISL runs its worker as a sidecar
in the web task and CMP has no always-on worker at all. Manageability is the
real driver, and the second-order costs (three mechanisms to debug, three
places a failure can hide) are the ones worth paying down.

## Non-negotiable constraints

Task code runs in its own project. A worker executes a task by importing it,
which requires that project's models, settings, and database connection. One
worker process is one Django settings module and one database. There is no
design in which a shared worker runs another project's tasks, and any proposal
that implies one is wrong.

What is shared is the model, the helpers, and the contract. Never the runtime.

Databases stay separate per project. Queues stay separate per project.

## Functional requirements

### R1 — One interface

All projects enqueue and define background work through `django.tasks`.
No project imports a queue backend directly. The backend must be swappable by
configuration.

### R2 — One compute shape

Every project runs its workers the same way: a worker process alongside the web
process, on the same platform, deployed by the same mechanism, in both
production and development environments.

Environment parity is an explicit requirement. Today it is broken in two
places: one project's scheduled jobs exist only in production, and another runs
Lambda in production and systemd in its sandbox, with a status page that only
understands the latter.

### R3 — Status that crosses service boundaries

A single logical operation can span two services:

```
project A "send campaign 42" → hub job → N child tasks → provider → events → callback
```

A correlation identifier minted by the originator must survive the API call,
the execution in the other service, and the callback. Asking "did it go out"
in the originating project must be answerable regardless of where the work ran.

This is a first-migration requirement, not a later addition. Threading an
identifier retroactively means backfilling across two services.

### R4 — Progress, modelled by workload shape

Three shapes, deliberately handled differently.

Countable fan-out. The parent knows the total before enqueuing children. This
is common: campaign sends chunk a materialised recipient list, event
notification fan-outs iterate registrations and already return their count,
imports iterate rows. These need a progress bar.

Opaque single unit. Most scheduled jobs, and every individual send. There is no
meaningful numerator. These need elapsed time measured against a rolling
baseline of previous runs of the same task, so the question "is it stuck" is
answerable. Raw elapsed time alone is not useful.

Deferred external outcome. Email opens and clicks arrive over hours to days and
never reach a terminal state. These are not task progress and must not be
modelled as such — doing so makes every campaign appear to run for a week and
destroys the meaning of "in progress". They belong on the domain object, fed by
the provider's event pipeline.

A campaign page renders both: send progress from the task system while sending,
engagement counters from the domain model forever after.

### R5 — Tenant scoping

One of the three projects is being built as a multi-client product and is
becoming the hub the other two send through. Its task records are not all
equal — a client must see only their own jobs.

The task model carries an optional owner field from the first migration.
Adding tenant scoping later means backfill plus auditing every existing query
for the missing filter.

### R6 — Per-task AWS credentials

A project's container role should not carry the union of every permission any
of its tasks needs.

Each project's task role becomes an almost-powerless identity whose only
permission is assuming per-task roles. Each task type has a role scoped to
exactly what it does, and the package resolves task name to role from one
declarative map:

```python
with task_role("<task-role-name>") as aws:
    client = aws.client("ses")
```

Requirements on this mechanism:

- Default-deny. A task with no map entry gets a session with no permissions,
  never the parent role. Otherwise new tasks silently inherit whatever the
  container has.
- The role session name carries the task run identifier, so audit-log entries
  join back to the task that caused them using the same identifier as R3.
- Credentials must refresh. Long fan-out parents outlive a default session
  lifetime, and mid-task expiry presents as a random permissions failure.
- No module-level cloud clients. Anything constructed at import time picks up
  ambient container credentials and bypasses the scheme entirely.

What this achieves: scoping per unit of work rather than per container, audit
attribution, and blast-radius reduction for bugs. What it does not achieve:
containment against a compromised worker, which can assume any of the roles.
It is not a tenant boundary and must not be described as one.

### R7 — One console

A single page showing, for every project: worker liveness, queue depth and
oldest pending age, registered schedules with last and next run, recent task
runs with status and progress, and a failure count.

The console reads a versioned JSON contract over HTTP from each project. It
holds no state beyond a cache and has no database.

Pull, not push. A console that polls learns a project is dead because the fetch
fails. A console that waits for pushes cannot distinguish a healthy quiet
project from a dead one.

The console deploys independently of the projects it watches. Embedding it in
the largest project is cheaper but means the tool used to diagnose an outage
dies with the thing most likely to be having one.

Paging stays on cloud-provider alarms, which survive the console being down.

### R8 — Transactional enqueue

Enqueuing must be able to participate in the database transaction that caused
it, so that a committed data change and its follow-up work cannot diverge.

Two of the three projects currently work around the absence of this: one wraps
every enqueue in an on-commit hook by hand, the other built a full outbox with
a six-state machine to get the same guarantee. Making it native removes both.

### R9 — Serialisation by lock, not by global concurrency cap

Rate-limited external providers need work serialised per key — per client, per
configuration set — not globally.

The current mechanism in one project is a global concurrency cap of one on
every worker, which serialises unrelated clients against each other. The
replacement must express the real constraint.

## Split: package versus contract

Package the write path, where divergence causes real bugs:

- backend wiring and adapter
- the task run model and its migration
- enqueue helpers stamping correlation, parent, and owner identifiers
- progress helpers
- per-task credential resolution
- periodic task registration
- the default status collector

Three separate implementations would diverge on what counts as failed, when a
heartbeat is written, and whether a correlation identifier survives a retry. If
"failed" means something different per project, the console does not aggregate
— it lies.

Contract the read path, where the network already forces a boundary:

- the status endpoint view and its authentication, which differs per project by
  nature
- the console itself
- per-project entity resolution, which imports that project's domain models and
  cannot move
- domain counters

Compatibility rules: package migrations stay additive for several releases,
never renaming or dropping a column in a minor version. The contract carries a
version field the console tolerates older values of. Each project then upgrades
on its own schedule, which matters because the hub project's release cadence
diverges from the others once it has external clients.

## Hub topology

The originating project owns who gets what and when. The hub owns render, send,
and track.

This keeps domain logic where the domain data is. Scheduled jobs that decide
who should receive something stay in the project that understands tiers,
registrations, and enrolment state. Only the final delivery step moves.

The hub exposes one generic job resource rather than per-feature status
endpoints, and the job payload uses the same shape as the internal status
contract, so the console renders a hub job with the code it already uses for a
local task.

An idempotency key supplied by the client is mandatory — a client retrying
after a timeout must not cause a double send.

### Status ownership

The hub is authoritative. Clients hold a projection that is explicitly a cache
and never a second source of truth. A split where both sides own status,
fan-out, and history produces reconciliation bugs.

Three parts, because callbacks alone are insufficient:

1. Callbacks maintain the projection, so a client page renders locally and
   survives the hub being slow.
2. A reconciliation sweep re-pulls any non-terminal chain after a bounded
   interval, because callbacks get lost.
3. An explicit refresh on the detail page, for when someone is watching.

## Migration stance

Migration cost is not a design constraint. Where the from-scratch answer
differs from evolving what exists, the from-scratch answer wins and the
migration is work to be done.

Two things this does not license:

Rewriting working application code. Infrastructure is disposable; tested domain
behaviour is not. Handlers that are already backend-agnostic lose their wrapper
and become plain task functions.

Big-bang cutover of anything irreversible. Email is the only unrecallable
output in this estate; a bad cutover sends the entire list duplicates or
nothing. It also needs to stay clear of the platform migration, because two
simultaneous changes make a breakage undiagnosable.

## Open questions

Deliberately unresolved, listed so plans do not assume an answer:

1. Whether a maintained backend adapter for the `django.tasks` interface exists
   for the chosen queue, or whether it is ours to write.
2. Real send volume at the hub, and the resulting connection count against a
   small database instance.
3. Whether the existing container size fits a web process plus a worker for the
   two projects that do not run one today.
