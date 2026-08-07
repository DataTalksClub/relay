# Relay — Unification Plan

Date: 2026-08-07

Ordered work to turn this repository into Relay. Read
[context.md](context.md) and [requirements.md](requirements.md) first.

Phases 1 and 2 are mechanical and reversible. Phase 3 is the new capability.
Phase 4 is infrastructure. **Keep the test suite green throughout** — it is the
only thing standing between a refactor and a silent behaviour change in a
service that sends real email.

Baseline, verified at the specification commit: **493 tests pass** and
`ruff check .` is clean.

Note that the two repositories had different ruff configurations — taskdeck
enabled `BLE001`, this project does not — so a `noqa` in the merged tree became
unused and was removed to get the baseline clean. Expect a few more of that kind
as the two configurations are reconciled in Phase 1; they are cosmetic, but do
not let them accumulate, because a permanently-failing lint stops being read.

---

## Phase 1 — Absorb taskdeck

Goal: `taskdeck` becomes an app in this repo rather than a git dependency.
No behaviour change.

1. `git mv taskdeck-src/taskdeck taskdeck` — the Django app. **Keep the app
   label `taskdeck`.** Its migration `0001_initial` is already applied in
   datamailer's database, and renaming the label means a migration rewrite for
   no benefit.
2. `git mv taskdeck-src/tests tests/taskdeck` — keep them; they cover the signal
   binding, which is subtle and easy to break.
3. `git mv taskdeck-src/docs/decisions.md docs/taskdeck-decisions.md` and
   `taskdeck-src/docs/contract.md docs/contract.md`. `taskdeck-src/docs/requirements.md`
   is superseded by this repo's — read it once for the reasoning, then delete.
4. Delete `taskdeck-src/` remnants: `pyproject.toml`, `README.md`, `uv.lock`.
5. In `pyproject.toml`, remove the `"taskdeck"` dependency **and** the
   `[tool.uv.sources]` entry pinning it to git. Fold in anything it declared
   that this project does not already have.
6. The `Dockerfile` installs `git` solely to resolve that git-pinned
   dependency. Remove that, and the `apt-get purge` that follows it.
7. `uv sync && uv run pytest`. Expect green with no code edits — the import
   path `taskdeck.*` is unchanged.

Verify: `grep -rn "taskdeck" pyproject.toml` returns nothing, and
`taskdeck` still appears in `INSTALLED_APPS`.

---

## Phase 2 — Rename the project to Relay

Goal: the Django project module becomes `relay`. Domain apps keep their names.

1. `git mv datamailer relay` — settings, urls, wsgi, asgi.
2. Update every reference to `datamailer.settings` / `datamailer.wsgi`:
   `manage.py`, `pyproject.toml` (`DJANGO_SETTINGS_MODULE` under
   `[tool.pytest.ini_options]`), `conftest.py`, `Dockerfile`, `Makefile`,
   `.github/workflows/`, `scripts/`.
3. `TASKDECK_PROJECT = "relay"` in settings — this is the identity the status
   contract reports and what a console keys on.
4. `[tool.ruff.lint.isort] known-first-party` → `["relay", "mailing", "taskdeck"]`.

**Do not rename:**

- the `mailing` app, or any `db_table`. The tables carry real data and the names
  are explicit in the models; renaming buys nothing and risks everything.
- environment variable names (`DATAMAILER_*`). Rename them later, deliberately,
  with the deployment — not in the same change as a module move.

Verify: `uv run pytest`, `uv run ruff check .`, and
`uv run python manage.py check`.

---

## Phase 3 — The generic job API

Goal: the capability Relay exists for. This is design work, not mechanical.
Do not start it until Phases 1 and 2 are green and committed.

Build in this order — each step is useful on its own:

### 3a. A job resource

One endpoint that accepts a task submission, returns an id, and one that
reports status. Payload shape mirrors the status contract, so a console renders
a Relay job with the code it already uses for a local task.

- Client-supplied `idempotency_key` is mandatory (R7). Reuse
  `find_existing_message`'s pattern from the email path.
- Scope every query by owner (R5). The API key already resolves to a client.

### 3b. Webhook tasks

The mechanism that removes per-project workers. A task type whose work is an
authenticated HTTP call to a client URL.

- **Sign the request.** HMAC over body and timestamp, per-client secret, so the
  receiver can verify origin and reject replays.
- **Pass the task id** so the receiver can deduplicate. Retries will happen.
- **Bound it with a timeout**, and document the ceiling. See open decision 1 in
  [context.md](context.md) — resolve it before building, since it decides
  whether an ack-then-callback path is needed now.
- Retry with backoff. Distinguish 4xx (client's problem — do not retry blindly)
  from 5xx and timeouts (do retry).
- Record the response status and a truncated body on failure. A webhook task
  that fails with no detail is undebuggable from the console.

### 3c. Schedules

Cron owned by Relay, firing a built-in or webhook task. Report last run, last
success, and whether a run was missed — a schedule that stops firing looks
exactly like a quiet period otherwise.

There is a working reference for the reporting half in `mailing/ops_views.py`
(`_schedules`) and `TASKDECK_SCHEDULES` in settings.

### 3d. Per-task roles (R6)

`mailing/aws.py` builds every client from ambient credentials today. Replace
with a resolver: task type → role ARN, assume, cache until shortly before
expiry, build clients from those credentials.

**Assume-role failure must fail the task.** It must not fall back to ambient
credentials — that silently does the work with the wrong identity, which is the
exact failure the scheme exists to prevent.

There is a related design already written up for the email half:
`DataTalksClub/aws-infra` issue #19 covers per-tenant SES sender roles.

---

## Phase 4 — Infrastructure

Goal: Relay runs on its own infrastructure. **Datamailer is untouched.**

Raise as a **pull request** against `DataTalksClub/aws-infra`. Do not apply.

- New root, e.g. `sandbox/relay`. Do not edit `sandbox/datamailer`.
- `eu-west-1`, per the promotion rule in `aws-infra/docs/state-boundaries.md`.
  Datamailer's `us-east-1` is legacy and must not be inherited by copying.
- Own queues, own SES configuration set, own database, own DNS name. Nothing
  shared with datamailer — a shared resource is a shared outage.
- Keep the two AWS-fed ingress queues (`ses-webhooks`, `inbound-email`); their
  producers are SNS and S3, so they cannot move onto the internal queue.
- An Elastic IP if the compute keeps a public address. Datamailer's does not
  have one, so a stop/start changes its IP and breaks DNS — do not repeat that.
- The worker must be in the deployment definition, and the deploy must fail if
  it is not running (R2).

Cost: the owner is explicit about keeping this low. Postgres in a container on
the app host costs nothing beyond the instance; RDS `db.t4g.micro` adds roughly
$12/month plus storage. Do not reach for RDS without asking.

---

## Phase 5 — Clients

Only after Relay is deployed and proven.

1. **DataTalksClub website** — first client using both halves. Design the job
   API against its real needs rather than guesses.
2. **CMP** — migrate from datamailer to Relay for email. This is a send path,
   so it moves gradually and reversibly, never as a cutover. Both point at the
   same SES identity, so a client can be switched by changing one URL.
3. **AISL** — general background work. Largest win, largest migration; not a
   first-release requirement.

Datamailer is retired only when nothing points at it, and not before.

---

## Standing rules

- `uv`, not `pip`.
- Keep the suite green. If a test asserts old behaviour, change it deliberately
  and say why in the commit — do not delete it to get to green.
- Do not push to `DataTalksClub/datamailer`. Its `main` auto-deploys to the host
  serving CMP production email.
- Infrastructure by pull request.
- Read the four mistakes in [context.md](context.md) before building the worker
  or the task layer. They are all cheap to repeat.
