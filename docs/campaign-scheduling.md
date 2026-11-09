# Campaign scheduling and composition

Campaigns can be saved as unfinished drafts, composed with heading/paragraph/button/divider blocks, or imported as HTML and plain text. The composer generates both email versions and saves its block state alongside the campaign. Source mode preserves imported HTML. JavaScript is optional for the source workflow; visual editing requires it.

Saving a draft does not send or schedule it. Choose a future local date/time and an IANA timezone, then confirm the schedule on the review page. The form rejects ambiguous and nonexistent clock-change times rather than guessing. API `scheduled_at` values must contain an explicit UTC offset, for example `2027-01-10T08:00:00Z`. Queueing a future campaign changes its state to `scheduled`; the API response includes `scheduled: true`.

The recipient estimate at review is provisional. At the due time, Relay snapshots eligible recipients using the current membership, subscriptions, consent, suppression, and tags. Changes before dispatch therefore affect the selected audience. A scheduled campaign can be cancelled before sending starts; emails already sent cannot be recalled. To change a confirmed schedule, cancel it and prepare a new draft.

Operator confirmation carries the reviewed draft revision. A changed subject,
content, or time requires renewed review. If the chosen time has passed before
confirmation, the operator must choose a future time or explicitly select Send
after review; a late Schedule click cannot silently become an immediate send.

The existing `run_relay_scheduler` command dispatches due campaigns alongside recurring jobs and recovery work. Actual processing begins after the chosen time on a scheduler tick, followed by the sending worker; the timestamp is not a guarantee of exact delivery time. Both services must be running. Migration `0030_campaign_scheduling` must be applied before code relying on the new scheduled lifecycle and dispatch outbox runs.

Due dispatch locks campaign state and records batch payloads in a durable `CampaignDispatch` outbox. Task insertion happens after commit; the scheduler recovers unenqueued batches after interruption or transport failure. Recipient state and provider identifiers protect duplicate task execution. Sender guards reject premature or unreleased campaign work, and cancellation uses the same campaign lock as sending. A due campaign with invalid content becomes failed with a readable scheduling error. A due campaign with no eligible recipients completes without sending mail.

Configuration and scheduling tests use mocked providers. They establish delayed-dispatch behavior, not provider acceptance or inbox delivery. Browser previews simulate widths and isolate content; they do not establish compatibility with every email application.

## Durable dispatch outbox and stuck-task detection (WP5)

- Every campaign send (immediate or scheduled) writes a `CampaignDispatch` intent in the same transaction as QUEUED. The on-commit hook is only a fast path; the scheduler pass (`recover_campaign_dispatches`, every ~15 s) creates the batch tasks if the process died between commit and enqueue. `CampaignDispatchBatch` is unique per (campaign, batch index), and the tasks are inserted in the same transaction as the intent's `enqueued_at`, so no batch is created twice.
- `db_worker` beats `TaskHeartbeat` every `RELAY_TASK_HEARTBEAT_SECONDS` (30) while a task runs. The scheduler fails any campaign batch task RUNNING with no beat for `RELAY_TASK_LEASE_SECONDS` (300): the task row becomes FAILED with `exception_class_path=relay.stale_task.FAILED_STALE`, an ERROR log `STALE CAMPAIGN TASK ...` is written, and a `CampaignTaskAlert` shows on the campaign page and as `dispatch_alerts` in the campaign API.
- `RELAY_STALE_BATCH_AUTO_REENQUEUE` (default **off**) re-enqueues only the still-PENDING recipients of a stale batch. Keep it off until WP1 (SES call outside the DB transaction) is deployed: before that, PENDING can mean "accepted by SES but rolled back", and a re-enqueue could send a duplicate. While off, stale batches are only reported.
