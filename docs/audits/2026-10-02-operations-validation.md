# Service operations validation — 2 October 2026

Reviewed the current Jobs, Schedules, Failed jobs, and Service health interface after the earlier audit implementation. These destinations show shared sandbox service records with explicit client identity; workspace selection does not silently narrow them.

## Corrections

- Jobs and schedules preserve client, state, search, and pagination filters when returning from a detail or a rejected action. Return URLs accept only the matching local operational list; external URLs and unrelated destinations fall back to the record's client list.
- Invalid or unknown client filters return no records and explain how to recover instead of silently widening to all clients.
- Retry and schedule actions retain native required-checkbox validation and server confirmation/revision checks. Rejected actions show an alert and move keyboard focus to it. Disclosure controls have visible focus indicators; local navigation identifies the current page and table headers have explicit column scope.
- Resuming a schedule with a corrupted cron definition produces an actionable conflict rather than a server error, leaving it paused.
- A retry transport outage leaves the committed job queued without a task result, available to the existing scheduler recovery sweep. It does not produce a misleading request failure after a committed state change. The interface continues to distinguish queued work from completed execution.

## Validation

**25 focused tests passed:** all jobs tests plus the webhook failed-job review regression. Coverage includes staff access, scope/filter handling, stale or incorrect confirmations, safe future-only resume, invalid cron, transport recovery, preserved return context, and usable HTML redirects. Ruff, Django system checks, and whitespace checks pass.

An isolated local browser walkthrough checked Jobs, Schedules, Service health, failed-job detail, and schedule detail at **1440, 390, and 320 pixels**: all 15 visits returned 200, fit the viewport, and had no duplicate IDs, unlabelled visible inputs/selects, broken description references, or JavaScript errors. Keyboard Enter opened the technical disclosure; Space toggled the confirmation checkbox; the native form rejected an unchecked confirmation. A deliberately incomplete retry request changed no job state, focused the error alert, and retained the original filtered return link. Evidence and mobile screenshots are in `.tmp/operations-validation/`.

No worker, scheduler, email send, real retry, or deployment was started. The browser used the synthetic audit database. Automated semantic checks and keyboard checks are evidence of these particular behaviors, not a substitute for testing with a human screen-reader user.

## Scheduler integration observation

The existing `run_relay_scheduler` command sweeps expired leases, recovers unenqueued jobs, and dispatches recurring schedules. Recurring schedules submit at most one job after downtime and advance to a future cron occurrence. Delayed campaigns should use a dedicated one-shot due-campaign dispatch with locked state transitions and send-time guards; recurring `Schedule` records should not stand in for one-shot campaign sends. This guidance was shared with the campaign scheduling implementer; campaign backend changes are outside this operational UI review.
