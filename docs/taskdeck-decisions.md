# Task status layer decisions

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

One path complicates it. The bulk member upsert that precedes a recipient-list
send dispatches inline rather than deferring, and its caller reads the
resulting status synchronously to decide whether the send may proceed. That is
a request-time gate, not background work, and it reaches the gate through a
wrapper rather than as a direct call, so it is easy to miss when counting
callers.

It does not block the deletion, but it changes the shape of it: that path
becomes an ordinary synchronous API call, and only the genuinely deferred paths
become tasks. Arguably clearer than routing a blocking check through queue
machinery, but it has to be done deliberately rather than discovered during the
cutover.

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

## Learned while building it

Recorded because each of these cost real time and none was predictable from the
design.

### The two tasks implementations have separate signal objects

Django 6.0 ships `django.tasks`. The `django-tasks` backport ships
`django_tasks`. Each defines its own `Signal` instances, and a backend emits on
whichever module it imported.

The database backend imports the backport's. Its compatibility shim aliases the
`Task` *class* so tasks defined with Django's native decorator run correctly —
but it does not alias the signals. So the obvious wiring, connecting receivers
to `django.tasks.signals`, produces a projection that stays permanently empty
while every other part of the system appears to work perfectly.

taskdeck binds to every signals module present and deduplicates on the
backend's result id. The test suite asserts the mismatched combination
explicitly, because that is the one a reasonable person would not think to try.

### The projection has to be defensive, not correct

A receiver that raises inside `task_enqueued` propagates into the caller's
enqueue. A missing status row degrades a dashboard; a raised exception loses
the user's work. Every receiver swallows and logs.

### Context must be bound inside a deferred callable

Binding a correlation id around a loop that calls `enqueue_on_commit` does
nothing: on-commit callbacks run after the block exits, by which point the
context manager has reset. The binding has to happen inside the deferred
function. This is easy to write wrongly and produces no error — just
uncorrelated rows.

### Enqueue-time context matters more than expected

`set_entity` and `set_owner` act on the currently executing task, so anything
merely queued carries nothing until a worker picks it up. That is exactly when
an operator wants to know what is waiting. `stamp()` annotates the row at
enqueue instead.

### Pinning by git tag has a build cost

A git-pinned dependency needs git present wherever the image is built, and
`uv sync` will not resolve it otherwise. It also means a change to the package
requires an explicit lock upgrade in each consumer before it is picked up —
easy to forget, and the failure surfaces as a stale attribute error rather than
anything obviously version-related.

Worth the isolation for now, but it is a real tax and the reason to publish to
an index once the API settles.
