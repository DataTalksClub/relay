# taskdeck

The task layer behind a central platform that runs background work for the
estate, plus the contract and console for watching it.

## The shape

One platform runs the work. Other services **use** it over an API — they ask
for a task type the platform implements, get an identifier, and watch the
status. They do not install this package to do that.

The platform owns every task implementation it executes. That is what makes one
runtime possible: a worker can never import a *client's* code, because it never
runs any.

Some work legitimately stays in the calling project — deciding *who* should
receive something needs enrolments and deadlines, which live there. This package
is what a project uses for that residue, and the console shows it alongside
platform work.

See [docs/requirements.md](docs/requirements.md) for the full statement,
including a record of an earlier draft that described this as a per-project
library and the confusion that caused.

## What the package provides

Django 6.0 ships `django.tasks` — a standard interface for enqueueing
background work — but no production worker. taskdeck builds on that interface
and adds what a small estate actually needs:

- a durable backend, without adding queue infrastructure
- task status that survives crossing a service boundary
- progress for fan-out work, elapsed-versus-baseline for everything else
- per-task AWS credentials instead of one role per container
- the status contract a console reads

Application code depends on `django.tasks`, never on taskdeck's backend. The
backend is a settings change.

## Two things that bite on adoption

Both happened in production, and neither is caught by unit tests on either side
of the boundary — they live at the seam.

**Deploy a worker.** Enqueuing without one succeeds, rows appear as queued, and
nothing ever runs. Assert the worker is active in the deploy, so its absence
fails the deploy instead of going quiet.

**Return plain data from tasks.** The backend stores return values as JSON. A
task returning a model instance raises *after* its work has committed — the
email is sent and the record says it failed.

## Layout

```
taskdeck/    the package: backend wiring, task run model, progress, collector
tests/       its suite
docs/        requirements, status contract, design decisions
```

The console described in R7 — one page showing every project's workers,
schedules and failures — **is not built yet.** It is the piece that delivers
"one central place" for observation, and its absence is why the estate can feel
decentralised even where the architecture is right.

## Status

Design and early implementation. The package is in use by one project.

- [docs/requirements.md](docs/requirements.md) — what it must do
- [docs/contract.md](docs/contract.md) — the status contract
- [docs/decisions.md](docs/decisions.md) — why it is built this way
