# taskdeck

A shared background-task layer for Django 6 projects, plus a console for
watching it.

Django 6.0 ships `django.tasks` — a standard interface for enqueueing
background work — but no production worker. taskdeck builds on that interface
and adds the things a small estate of Django services actually needs:

- a durable backend, without adding queue infrastructure
- task status that survives crossing a service boundary
- progress for fan-out work, elapsed-versus-baseline for everything else
- per-task AWS credentials instead of one role per container
- a single console showing every project's workers, schedules, and failures

Application code depends on `django.tasks`, never on taskdeck's backend. The
backend is a settings change.

## Layout

```
taskdeck/    the package installed by each Django project
console/     the dashboard service — reads the status contract over HTTP,
             holds no state, deploys separately
docs/        requirements, contract, and design decisions
```

The package and the console live together because the status contract is the
thing they share — producer on one side, consumer on the other. The console
does not import the package.

## Status

Design stage. See [docs/requirements.md](docs/requirements.md) for what it must
do, [docs/contract.md](docs/contract.md) for the status contract, and
[docs/decisions.md](docs/decisions.md) for why it is built this way.
