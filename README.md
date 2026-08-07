# Relay

One service that runs background work for the estate. It sends email, and it
runs everything else. Other projects **use** it over an API — they do not
install it and they do not deploy workers of their own.

**Status: specification.** The code here is datamailer and taskdeck, merged with
their histories intact but not yet unified. Nothing has been renamed and no new
capability exists yet.

## Start here

Read in this order:

1. **[docs/context.md](docs/context.md)** — what Relay is for in the owner's own
   framing, the estate around it, what is deployed, and four mistakes already
   paid for in production. Written for someone with no prior history.
2. **[docs/requirements.md](docs/requirements.md)** — what Relay must do. The
   section that matters most is "Three kinds of task".
3. **[docs/unification-plan.md](docs/unification-plan.md)** — the ordered work,
   in phases.

## The idea in one paragraph

There are several Django projects in the estate and each one currently needs its
own worker process deployed and paid for. A central worker cannot simply import
another project's tasks — that would need its models, settings and database. But
the unit of work does not have to be a Python import: Relay's worker can make an
authenticated, retried, scheduled HTTP call into a project's existing web
process. The project adds an endpoint, not a deployment. Relay owns the queue,
the retries, the schedule and the status; the project keeps its code.

## What is in here now

```
datamailer/      Django project (settings, urls, wsgi) — to be renamed `relay`
mailing/         the email domain app — keeps its name and its table names
taskdeck-src/    the taskdeck repo, merged; to be unpicked into place
cli/ docs/ infra/ scripts/ static/ templates/ tests/
```

Both projects' histories are present — 200 commits — so `git log` still answers
why each part looks the way it does.

## Two things that must not be forgotten

**`DataTalksClub/datamailer` is in production use.** CMP production email points
at it (`main/cmp/app_prod.tf`), despite it being called a sandbox. Its `main`
branch auto-deploys. Relay shares nothing with it and does not touch it.

**Infrastructure changes go to `DataTalksClub/aws-infra` as a pull request**,
never applied directly.

## Conventions

`uv`, not `pip`:

```bash
uv sync
uv run python manage.py migrate
uv run pytest
```

`AGENTS.md` carries the conventions inherited from datamailer. Note that its
claim that datamailer has no production deployment is misleading — see
[docs/context.md](docs/context.md).
