# Recurring Sends

Deadline reminders go out on a timer. This records where that timer lives, why
it is not in Datamailer, and what Datamailer does instead.

## Where the schedule lives

EventBridge, on the CMP side. `aws-infra` `main/cmp/cmp_deadline_reminder.tf`
runs CMP's `send_deadline_reminders` management command daily at 09:00 UTC as a
Fargate task. That command works out who is due a reminder and posts one bulk
send to `/api/transient-recipient-lists/transactional-send`.

That is the source of truth for the schedule. Changing when reminders go out is
a Terraform change in `aws-infra`, not a change here.

## Why Datamailer does not own it

It was tempting to add a scheduler here, since Datamailer is the thing that
sends the mail. It would be the wrong split.

Deciding *who* is due a reminder needs the course model: deadlines, enrolments,
submissions, and who has already been reminded. All of that lives in CMP.
Datamailer knows about contacts and templates, not about homework. A scheduler
here would have to either call back into CMP to ask, or hold a copy of CMP's
deadlines — a second source of truth for something that changes often.

The current split keeps each side doing what it has the data for: CMP decides
the recipients and the timing, Datamailer renders and delivers. The schedule
sits next to the data that determines it.

There is also a smaller reason. A scheduler needs a leader: with more than one
worker, something must ensure a daily job fires once rather than once per
worker. EventBridge already solves that. Rebuilding it here would mean solving
it again, for one job.

## What Datamailer contributes

A schedule that stops firing is invisible from the sending end — no mail is
sent, but no mail is sent on a quiet day either. Both look identical.

Two things close that gap.

**One run per sweep.** The bulk endpoints enqueue a single batch task that fans
out to the individual sends, rather than N unrelated sends (see
`mailing/tasks.py::send_transactional_email_batch`). A sweep is therefore one
row with `k/N` progress, not 300 anonymous ones.

**A declared expectation.** `TASKDECK_SCHEDULES` in settings names each
recurring send, the template it uses, and how often it should happen. The
status contract reports the last matching batch against it, so a missed sweep
shows as a stale `last_success` instead of silence. `max_age_s` is the field a
console should alarm on.

Keep `cron` and `max_age_s` in step with the EventBridge rules. They are a
statement of what the trigger is expected to do, and a stale value here makes
the check assert the wrong thing.

## If it ever should move

The case for moving scheduling into Datamailer is a second caller wanting
recurring sends without building their own trigger. If that happens, the shape
to add is a schedule table plus a leader-elected tick, and the CMP job becomes
a row in it rather than an EventBridge rule. It is not worth building for one
caller that already has a working trigger.

## Adding another recurring send

1. Create the trigger where the deciding data lives — for CMP, an EventBridge
   rule alongside the existing one.
2. Have it post to a bulk endpoint, so the sweep gets a batch parent and
   progress. A loop of single sends gets neither.
3. Add an entry to `TASKDECK_SCHEDULES` with the template key, so a missed run
   is visible.
