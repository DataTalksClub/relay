# Relay UX completion — 2 October 2026

Implemented the remaining recommendations from the [operator UX audit](2026-10-02-operator-ux-audit.md), building on the [first pass](2026-10-02-ux-implementation.md) and the updated interface. The existing Django templates and shared visual styling remain the foundation. Changes are local; they have not been deployed to the sandbox.

## What changed

The campaign operator can save an unfinished draft, compose with heading/paragraph/button/divider blocks or imported HTML, resume it, review eligible recipients and exclusions, inspect the actual rendered email, send an explicitly confirmed test, and confirm immediate or scheduled delivery. The composer generates plain text and preserves editable block state. Test sends use the existing sending service and do not create an audience recipient snapshot. Preview controls support desktop and mobile widths. A changed recipient count requires another review. Cancellation requires confirmation and is available only before sending has begun; sent messages cannot be recalled.

Scheduled campaigns have an explicit lifecycle, local date/time and IANA timezone, clock-change validation, and due-time recipient selection. The existing scheduler dispatches them through a durable outbox, with recovery after transport interruptions, duplicate-task protection, early-send guards, and cancellation serialized against sending. Review discloses that membership and consent changes before dispatch can change the audience. See [campaign scheduling and composition](../campaign-scheduling.md).

Support can start from one Email activity destination for both transactional and campaign messages. Search and filters cover recipient, subject, message type, state, period, campaign, and template. Results lead to the relevant message or campaign recipient while preserving investigation context. Provider acceptance and delivery confirmation remain distinct; overlapping historical metrics are explained rather than presented as an additive funnel.

Contacts and audiences lead with people and effective eligibility. Audience Members, Segments, Campaigns, Activity, and Health are separate sections. Scoped eligibility totals use database queries rather than loading every member. Filter chips remove one constraint at a time, and returning from a contact preserves the search. Global contact suppression changes now require a signed, time-limited before/after review; stale or altered confirmations are rejected.

Integration setup groups senders, API keys, optional connected services, and diagnostics. Sender rows and a default selector replace mandatory line-format editing, with advanced import retained. Readiness distinguishes configuration from observed delivery. Developer documentation starts with a quickstart and separate workflow pages, with copyable examples and a searchable, paginated endpoint reference. Existing reference links remain available.

Service operators have Jobs, Schedules, Failed jobs, and Service health routes. Failed-job retries and schedule pause/resume require confirmation and reject stale revisions. Service health explains unavailable monitoring. Unsupported operations are not offered as controls.

## Audit coverage

| Finding | Implemented response |
| --- | --- |
| UX01: misleading sendability | Effective campaign eligibility and blocking reasons; consent remains separately visible. |
| UX02: misleading scheduling | Real delayed dispatch, explicit scheduled state/timezone, due-time eligibility, early-send guards, durable recovery, and cancellation before sending. |
| UX03: broken client switching | Valid destination lists, browser-history reconciliation, unsaved-change protection, and stale scoped-form rejection. |
| UX04: dashboard hierarchy | Actionable issues and active work precede inventory and diagnostics. |
| UX05: campaign preparation | Incomplete drafts, recipient/message/review flow, persistent summary, actual preview, confirmed test send and audience submission. |
| UX06: delivery investigation | Unified searchable campaign and transactional Email activity with outcome-first details. |
| UX07: audience density | Members first; separate local sections for reports and history. |
| UX08: inconsistent scope | Daily client work separated from shared service work; global changes disclose their reach. |
| UX09: narrow-screen triage | Responsive identity/state/issue rows and wrapping labels. |
| UX10: ignored audience client filter | Explicit selected-client scope, consistent counts, and working filters. |
| UX11: duplicate form IDs | Unique form IDs, labels, help/error associations, and linked error summaries. |
| UX12: global suppression edits | Before/after review with explicit confirmation and stale-state protection. |
| UX13: incidents versus exclusions | Actionable failures separated from routine policy outcomes, with readable explanations. |
| UX14: template approval | Isolated visual preview, editable sample JSON, required-variable feedback, and width controls. |
| UX15: scattered setup | Sender rows, default selection, readiness, focused settings sections, optional integrations. |
| UX16: operational visibility | Jobs and schedule screens, confirmed retry/pause/resume, visible service health. |
| UX17: insider language | Consistent Relay operator branding, practical lifecycle copy, actionable empty states. |
| UX18: oversized reference | Quickstart, workflow guides, searchable paginated endpoint reference, copy controls. |

## Rendered evidence

Browser verification used an isolated SQLite database with synthetic demo records. No external emails were sent and no worker jobs were started. Screenshots are temporary hosted copies and expire after 60 days:

- [Audience members](https://d31nukezbn4e3o.cloudfront.net/2026/10/02/77967eb25ce0447abd9a769d2028b8f9-71fafcadf37e2dd2.png)
- [Mobile Email activity](https://d31nukezbn4e3o.cloudfront.net/2026/10/02/9f760a1b9aaf48c393bfbff187a2577e-0d03dfb24cdd3b0f.png)
- [Developer quickstart](https://d31nukezbn4e3o.cloudfront.net/2026/10/02/b46a3747b32344b489dc6526d3afe65f-6244509f7a58a267.png)
- [Global contact change review](https://d31nukezbn4e3o.cloudfront.net/2026/10/02/bed1f68d746a472db26ba60512ece9da-b6a58dcb87ca69fb.png)

Transient captures, DOM measurements, and workflow results are in `.tmp/ux-completion/`. Original audit evidence remains in `docs/audits/assets/2026-10-02/`.

## Validation and practical limits

The final full regression run passed **922 tests** in 171 seconds. This supersedes the earlier 872-test and intermediate 914-test completion passes. Coverage includes eligibility agreement, scoped navigation and stale submissions, unified activity filtering, incomplete drafts and send readiness, preview isolation, confirmed test sends and cancellation, signed global-state review, sender setup, documentation routes, confirmed job/schedule operations, actual delayed dispatch, outbox recovery, timezone/DST validation, composer injection protection, persisted editing state, and locked stale-review/expired-schedule guards. Sending tests mock the provider.

Ruff, fresh-process Django system checks, migration consistency, and whitespace checks pass. Campaign views, form, template, scheduling service, and sender checksums remained unchanged throughout the final full run. Existing admin API behavior and concurrent shared styling/status-badge improvements were preserved. File ownership with the separate migration coordinator was agreed using the [agent coordination skill](../../../.agents/skills/a2a-communication/SKILL.md); that coordinator did not edit these files or run competing deployments/imports.

Rendered checks cover 41 captures at desktop, 390px and 320px widths. They returned successful responses with no viewport overflow, duplicate IDs, unlabelled visible controls, broken help associations, or JavaScript errors. Separate workflows verified client-switch cancellation, sender row editing, editable previews, global-change review and cancellation, and schedule pause/resume. Campaign review includes the actual message preview and explicit recipient confirmation; mobile email preview measures 375px. Sampled computed text contrast in both themes covers 18 page/theme combinations and 2,026 text nodes with no violations; this is not a complete accessibility conformance assessment. Keyboard verification covers the skip link and main-content focus.

Additional browser workflows verified composer reordering and focus, 375px live preview, escaped user content, draft resumption, scheduled review/confirmation/cancellation, clock-change error focus, responsive navigation, and source editing with JavaScript disabled. The composer fits 1440px, 390px, and 320px without overflow, duplicate IDs, unlabelled visible controls, or JavaScript errors. Local evidence is in `.tmp/ux-completion/remaining-browser.json`.

Latest local captures: [desktop composer](assets/2026-10-02/completion/composer-1440.png), [mobile composer](assets/2026-10-02/completion/composer-390.png), and [scheduled campaign](assets/2026-10-02/completion/scheduled-320.png).

Larger synthetic datasets and operational error/keyboard flows were also checked. See [UX validation](2026-10-02-ux-validation.md) and [operations validation](2026-10-02-operations-validation.md) for the measurements and limits. Unnecessary audience queries were removed, history selectors bounded while preserving selected values, and sender/documentation/operation focus and error handling improved.

Roles and task priorities remain hypotheses from the audit. Actual-user sessions, a human screen-reader walkthrough, authenticated sandbox verification, and real email-client compatibility testing require people and access not exercised here. Synthetic SQLite timings do not establish the performance of the actual sandbox PostgreSQL database. There is currently only a sandbox deployment. This implementation has not been deployed; migration `0030_campaign_scheduling` is required before using scheduled dispatch in a deployed environment.
