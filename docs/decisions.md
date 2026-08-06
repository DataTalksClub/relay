# Design decisions

Recorded with reasoning, including the ones that were reversed during design.

## Build on `django.tasks`, not on a queue library

Django 6.0 ships `django.tasks`: a standard interface for defining and
enqueueing background work, with `TaskResult` carrying status and attempt
count. It ships only `dummy` and `immediate` backends, both explicitly for
development and testing. There is no worker in core.

Application code targets that interface and never imports a backend. The
consequences are worth more than any specific queue choice:

- the backend is swappable by configuration, per project and over time
- when a durable backend lands in core, adopting it is a settings change
- the projects are consistent because they target a published standard, not a
  convention we invented and have to defend

This decision was not obvious at the start. The first draft of this design
assumed django-q2 simply because one project already ran it. That is inertia,
not a reason.

## Backend: a Postgres queue, not a broker

Requirements that decide it: no new infrastructure, transactional enqueue (R8),
serialisation by lock rather than global concurrency cap (R9), and periodic
tasks.

One caveat, since it was initially stated as a decisive factor and is not: the
concern about polling load on a small database instance came from an alarm
threshold in a template that was never applied. It is not evidence about any
running system. A Postgres-native queue is still the right call on the other
four grounds, but that particular argument should not be repeated.

A harder prerequisite: a Postgres queue needs Postgres. One of the three
projects has no managed database at all and runs on SQLite, so provisioning one
is a precondition of this choice rather than a detail. Any plan that assumes
otherwise is planning against infrastructure that does not exist.

A Postgres-native queue using `LISTEN`/`NOTIFY` with `FOR UPDATE SKIP LOCKED`
satisfies all of these without adding a broker. Workers wake on enqueue instead
of scanning on an interval, rows are claimed without lock contention, and the
job insert commits with the data change that caused it.

Celery was considered and rejected: it needs a broker to run and pay for, and
its advantages — canvas, ecosystem breadth — are not what these workloads need.
The reference database backend for `django.tasks` was considered as the
low-risk alternative; it polls, and lacks locks. Because everything targets
`django.tasks`, choosing wrong costs a settings change rather than a rewrite.

Open: whether a maintained adapter exists for the chosen queue, or whether it
is ours to write. The backend interface is small, so it is contained work
either way, and it belongs in this package regardless.

## Progress as parent and child records, not a group counter

An earlier draft used the queue library's group-count primitive: enqueue
children in a named group, count completions in that group.

Parent and child task records are better and the reasoning generalises:

- backend-independent, so it survives a backend swap
- per-child status and error detail, which a counter cannot give
- costs nothing extra, because every task writes a record anyway

The parent stores the total, which it already computes before enqueuing.
Progress is the count of finished children.

## Pull, not push, for the console

A console that polls learns a project is dead because the fetch fails. A
console that waits for pushes cannot distinguish a healthy quiet project from a
dead one — silence looks identical in both cases.

Push would also need delivery guarantees to be trustworthy, which means an
outbox, which is machinery this design is trying to remove.

Pull costs staleness bounded by the poll interval. For a health view that is
the right trade.

## Console deploys independently

The cheaper option is embedding it in the largest project, reusing that
project's operator UI and authentication. That was the initial recommendation
and it was wrong: it means the tool used to diagnose an outage dies with the
service most likely to be having one.

It holds no state, has no database, and reads three HTTP endpoints. It should
be the least interesting service in the estate.

## Two layers of status, kept apart

Execution status and domain outcome have different lifecycles and must not
share a model.

A send task finishes when the provider accepts the message: bounded, minutes at
most, with a known denominator. Opens and clicks arrive over hours to days and
never reach a terminal state.

Modelling the second as task progress makes every campaign appear to run for a
week and destroys the meaning of "in progress" on the console. Execution status
lives in the task record; outcomes live as counters on the domain object.

A campaign page renders both, from two sources, and that is correct rather than
a compromise.

## Correlation and tenant fields exist from the first migration

Both are cheap now and expensive later.

A correlation identifier added afterwards means backfilling across two
services. A tenant field added afterwards means backfill plus auditing every
existing query for the missing filter, which is how a leak gets introduced.

Neither is speculative: the hub topology is the stated direction, and the hub
is being built as a multi-client product.

## Per-task credentials are attribution, not containment

The container role becomes an assume-only identity; each task type gets a role
scoped to its actual needs.

Stated honestly: a compromised worker can assume any of the roles, so this is
not a security boundary and must not be described as one. What it gives is
scoping per unit of work rather than per container, audit-log attribution via
the session name, and a bug in one task that cannot reach another task's
resources.

Worth doing. Not worth overselling.

## Deleting an outbox rather than porting it

One project built a database outbox with a six-state machine to guarantee that
a committed data change produces its follow-up work, and to retry a failing
external call.

Transactional enqueue provides the first natively; a retry policy provides the
second. The reason the outbox exists goes away, so the machinery is deleted
rather than re-mechanised onto the new system. The audit records and domain
events it produced survive as domain data.

This is the clearest single win in the design, and it only became visible after
dropping the assumption that the queue had to be the one already in use.

## Infrastructure is disposable, application code is not

The migration stance is that migration cost does not constrain design. That
licenses rebuilding infrastructure — stacks, queues, function definitions, unit
files — rather than carefully migrating it.

It does not license rewriting tested domain behaviour. Handlers that are
already backend-agnostic lose their wrapper and become plain task functions.
Senders, queue contracts, webhook processing, and tenant models stay.

One caveat discovered while reviewing this: verification strategies must be
checked against the code before being planned around. An earlier draft assumed
a capture mode existed for shadow-comparing rendered output during a cutover.
It was added and then removed again, so it is a feature to build, not one to
rely on.
