# Relay operator UX audit and improvement plan

Audited on 2 October 2026. This report covers the current Relay web interface, which still uses the Datamailer name in its main navigation.

The interface asks people to interpret the service's data model before they can do their work. Campaigns, subscriptions, verification, delivery events, queues, callbacks, and configuration are visible, but their relationship to a person's immediate task is often unclear. The first improvement should be to make every screen answer a practical question and provide the next useful action.

Recommended order: correct misleading states and broken context changes; simplify the home and detail pages; provide a campaign review flow and searchable delivery history. Keep the existing Django templates and CSS. A framework replacement is unnecessary for these changes.

## Scope and evidence

This is a heuristic audit and a browser walkthrough, not research with actual users. The roles and task frequencies below are hypotheses inferred from the working application and repository. Validate them before committing to a large redesign.

- Inspected routes, templates, forms, context selection, queueing, sending, and presentation logic in the current working tree, including local changes already present when the audit began.
- Created a separate SQLite database under `.tmp/ux-audit-2026-10-02/`, migrated it, and ran the existing local demo seed command. No existing database was modified and no sends or external writes were initiated.
- Used headless Chromium at 1440 × 900 and 390 × 900. Inspected 19 distinct page routes, six additional mobile captures, a template in dark mode, and contact results at 320px width. Checked context switching and selected form semantics.
- The sandbox at `https://relay.dtcdev.click` redirected to login. Findings describe the current local interface; deployment parity and authenticated sandbox behavior are unverified.
- No participant interviews, task-time baseline, screen-reader walkthrough, full contrast audit, large-data performance evaluation, or actual delivery tests were performed. Inbound list was rendered empty; populated inbound details were reviewed in source.

Selected browser evidence is saved in [assets/2026-10-02](assets/2026-10-02/), including [page measurements and focused checks](assets/2026-10-02/measurements.json). Complete transient captures and DOM measurements are under `.tmp/ux-audit-2026-10-02/`. Page lengths depend on demo content and the tested viewport; they are evidence of hierarchy problems, not usability scores.

## People and jobs to be done

One person may perform several of these roles. Organize around tasks before introducing separate role dashboards or permission systems.

| Likely user | Trigger and job | What they need to know | Successful outcome |
| --- | --- | --- | --- |
| Campaign operator or community manager | “I need to announce a course to the right subscribers.” | Audience, eligible recipients, exclusions, sender, actual email appearance, timing | A reviewed campaign goes to the intended recipients at the intended time. |
| Support or delivery operator | “A learner says their confirmation email never arrived.” | The relevant message, delivery state, reason, whether anything can safely be done | Explain the outcome and take a valid recovery action without duplicating mail. |
| Contact administrator | “Why is this person excluded, and should that change?” | Effective eligibility in this audience and client, consent scope, verification, validation, suppression | Make an explicit scoped change and see its effect. |
| Integration developer | “I need to configure and verify a service integration.” | Sender setup, keys, template variables, request examples, recent results | Complete a test and know the integration is ready. |
| Service operator | “Something is stuck or a scheduled job stopped.” | Affected client, oldest waiting work, last success, failure reason, recovery options | Locate and resolve the incident without interpreting every infrastructure counter. |
| Mailbox operator | “I need to read incoming messages and stop unwanted mail.” | Sender, subject, body, attachments, read state, blocking scope | Triage mail and apply the intended sender rule. |

The campaign operator's model is **recipients → message → review → send → result**. The support operator's model is **person/message → outcome → reason → action**. Neither naturally starts with a queue, provider event, or context contract.

## Improvements already present

The [July audit](2026-07-03-operator-ux-audit.md) is useful history, but several of its findings have changed:

- Transactional queue is now in the sidebar and its empty state mentions other clients' queued messages.
- Campaign sending has a confirmation step with estimated recipients and skips.
- Draft campaigns omit post-send statistics. Sent statistics have delivery, engagement, and progress groups.
- Contact details lead with marketing and transactional eligibility.
- Audience summary counts have several useful drill-down links; advanced filters start collapsed.
- Worker process details and many provider IDs are behind disclosures. Mobile navigation can collapse.

Preserve these improvements. The remaining problem is information ranking, workflow continuity, and the accuracy of the simplified explanations.

## Priority findings

Priority reflects plausible task impact, not observed frequency: **P0** can mislead a consequential decision; **P1** substantially obstructs a core task; **P2** adds recurring friction. Effort estimates are relative: S is a focused presentation or routing change, M spans several views and interactions, L requires a new workflow and backend work.

| ID | Priority | Finding | Evidence and affected job | Recommended change | Effort |
| --- | --- | --- | --- | --- | --- |
| UX01 | P0 | “Sendability” does not report effective eligibility | Audience rows show green “Subscribed” for an unverified contact. `primary_status_badge` simply returns the subscription badge; verification and validation are hidden behind `+2`. | Calculate effective marketing eligibility for the selected scope. Show “Cannot send — unverified” or the actual blocking reason; present subscription separately. | M |
| UX02 | P0 | Scheduling suggests behavior the send path does not enforce | Campaign form accepts “Scheduled at”; details show “Scheduled”. The inspected queue function immediately enqueues batches and the sender does not check `scheduled_at`. This is source evidence, not an observed timed send. | Immediately stop implying automatic delayed sending. Either remove/qualify the field as informational, or implement and verify delayed dispatch with visible timezone, cancellation, and distinct scheduled state. | S containment; L scheduling |
| UX03 | P1 | Changing client from a record breaks navigation | From `/campaigns/1/`, switching Newsletter → Courses preserves the same URL, resulting in HTTP 404. Scope is stored in the session. | Switch to the equivalent destination list when the record is unavailable. Carry scope in URLs or enforce an explicit record/client reconciliation policy. | M |
| UX04 | P1 | Important work appears below low-value counters | Dashboard has 13 statistic tiles. “Deliverability Attention” begins around y=855 desktop and y=1959 mobile, below infrastructure backlog summaries. | Put actionable issues and work in progress first; compress service status; move inventory and detailed diagnostics to their destinations. | M |
| UX05 | P1 | Campaign preparation is a long technical form without an email review | Five sections; approximately 1994px tall desktop. HTML and text are required before saving. Detail has a recipient preview, but no rendered email preview or UI test-send action. | Allow incomplete drafts; provide recipients, content, and review stages with a persistent draft summary. Connect existing preview/test-send capabilities to the UI. | L |
| UX06 | P1 | Delivery investigation has no complete starting point | Transactional queue only lists waiting messages. Past messages are reached via contacts or template recent-message tables. | Add Email activity covering queued, sent, delivered, skipped, failed, bounced, and complained messages, searchable by recipient and time. Keep queue as a filtered view. | L |
| UX07 | P1 | Audience page puts people behind reports | 12 tiles plus segmentation before membership; total page about 4719px desktop and 5883px mobile with 12 demo contacts. | Default to Members with compact health counts. Put campaign history and activity on separate sections reachable by visible local navigation. | M |
| UX08 | P1 | Client scope looks universal but is inconsistent | Mailbox and receiving addresses are global yet sit inside “Client” navigation. Audience belongs to an organization; contact flags can be global. | Separate client work from shared service work. Label global flags and organization-owned audiences at the decision point. | M |
| UX09 | P1 | Dense desktop tables become sideways investigations | Mobile contact results expose only email and part of status; last activity and issue require horizontal scrolling. Dashboard campaign table is 760px inside a 528px desktop column. | Use concise responsive rows for triage: identity, effective state, issue, latest event. Put detailed timestamps and diagnostics in the record. | M |
| UX10 | P1 | Audience client filter offers a choice that is ignored | Browser request `audiences/1/?client=2` returns selected client 1. The view forces the active client while the control still offers “Any client”. | Replace the control with a read-only scope label, or deliberately support the selected client and update all counts consistently. | S |
| UX11 | P1 | Contact forms duplicate IDs | Browser found two `id_audience` IDs, leaving an audience select without an associated label. | Give each Django form a prefix and verify labels, error links, keyboard order, and unique IDs. | S |
| UX12 | P1 | Subscription and suppression edits do not explain their reach | “State” exposes global unsubscribe, hard bounce, and complaint checkboxes alongside ordinary settings. Marketing eligibility says “eligible in at least one subscription scope”. | Present scope-specific eligibility; put global overrides behind an explicit action showing who is affected and the before/after state. | M |
| UX13 | P2 | Alert list mixes incidents and normal policy outcomes | Dashboard includes expected skips and unsubscribes alongside failures; no resolution or recommendation is shown. Complaint metadata can still display `{"feedback_type": "abuse"}`. | Separate actionable incidents from expected exclusions. Show reason and next step; retain raw metadata in diagnostics. | M |
| UX14 | P2 | Template preview is hard to use for content approval | Variables and example data precede preview. HTML is inserted into `<pre><code>` with app styling; no isolated preview frame or editable example inputs. | Show visual email preview first, with desktop/mobile sizing and editable sample variables. Keep source and API contract as secondary views. | M |
| UX15 | P2 | Integration setup is distributed through a long detail page | Client detail is about 2737px; callbacks and disabled Mailchimp mapping forms appear before API keys. Sender config requires line-based syntax. | Group Senders, API keys, Integrations, and Diagnostics. Use sender rows and a default selector; show optional integration setup when enabled. | M |
| UX16 | P1 | Relay jobs have no visible operational route | `/jobs/dead-letters/` exists and renders, but has no sidebar entry. Task and schedule APIs exist without equivalent general UI screens. | Link “Failed jobs” immediately under service operations. Add jobs and schedule visibility if service operators need it, without extending campaign pages. | S link; L full views |
| UX17 | P2 | Names and empty states require insider knowledge | App chrome says Datamailer; inbound says Relay. Labels include “Queueable”, “Context Contract”, “No send time”. Empty template state says “Adjust the client filter or seed…” despite no filter on that page. | Use consistent product naming and practical copy; each empty state explains scope and offers an action that actually exists. | S |
| UX18 | P2 | Developer reference overwhelms the initial task | API docs span approximately 25,707px in this local build. Navigation has workflow anchors, but the reference remains one long page. | Lead with a short quickstart and separate workflow pages from a searchable endpoint reference. | M |

## Proposed information architecture

Keep stable destinations and a visible selected integration. “Integration” is a proposed operator-facing replacement for “client”; confirm the term with users and retain client terminology in the API where needed.

| Navigation group | Destinations | Scope and purpose |
| --- | --- | --- |
| Daily work | Overview, Campaigns, Contacts, Audiences, Email activity, Templates | Selected integration. Audiences remain organization-owned and explain which client's membership is displayed. Templates remain explicitly transactional until campaign templates are supported. |
| Integration settings | Senders, API keys, Connected services | Selected integration. Developer docs are linked from relevant setup tasks. |
| Shared service | Inbox, Receiving addresses, Jobs, Service health | Explicitly service-wide. Show client labels on records where relevant. |
| Utilities | Help/API reference, theme, advanced admin | Secondary navigation. Retain Django admin for advanced maintenance. |

Audiences belong next to Contacts because maintaining recipients is daily work. Receiving addresses belong with Inbox. Email activity should answer “What happened?”; an internal queue should not be the only prominent message destination.

Do not initially create separate interfaces for every role. Make the common paths easy, preserve direct links and filters for experienced users, and use research to determine whether users need different default views.

## Screen and workflow specifications

### Overview

Top: integration name, search shortcut for a contact/message, and Create campaign. Then an action list containing affected record, plain-language problem, age, and next step. Below that, show sending campaigns and recent drafts with Resume or View results. Finish with a small recent delivery summary and service-status link.

Illustrative issue copy: “Course welcome email to Alex failed. Sender address is not configured. Configure sender.” Only offer Retry when the delivery result is known and retry is safe. Expected exclusions can say “14 recipients excluded by subscription policy — view reasons” without implying an incident.

When monitoring is unavailable, say “Processing health unavailable” rather than presenting unknown health as success. Show oldest waiting age and data freshness where useful; waiting count alone does not explain whether something is stuck.

### Campaign preparation and review

1. **Recipients:** choose audience, default to all eligible members, optionally narrow by tags. Show included and excluded counts, exclusion reasons, and recipient samples. Only offer tags from the selected audience. Make all-tags versus any-tag behavior explicit.
2. **Message:** subject, optional inbox preview, sender summary, and HTML import/source with a visual preview. Keep a text alternative editable. Save incomplete drafts; validate completeness before sending.
3. **Review:** show integration, sender, subject, actual rendered email, eligible recipients, exclusions, and timing together. Provide Preview and Send test through existing backend capabilities, respecting their recipient restrictions. A browser preview does not establish compatibility with all email clients.
4. **Commit:** use one unambiguous action, such as “Send to 842 recipients now”. If real scheduling is implemented, use “Schedule for 9 Oct, 09:00 Europe/Berlin”. Include an opportunity to return to editing.
5. **Outcome:** confirm accepted work and show progress. Distinguish accepted/queued, sending, sent to provider, and delivered. Never describe queue acceptance as successful delivery.

Recalculate recipients at review and submission, disclose material count changes, and prevent double submission server-side. Reuse existing queue protections. If cancel is supported, explain that messages already sent cannot be recalled; do not imply a universal undo.

The current HTML-import workflow can remain. Prioritize draft saving and review before considering a full visual email editor.

### Campaign detail by lifecycle

| State | First screen | Secondary information |
| --- | --- | --- |
| Draft | Ready-to-send checklist, recipient estimate, subject/preview, Resume and Review | Recipient exclusion breakdown, history |
| Scheduled, if implemented | Exact date/time/zone, audience snapshot policy, edit/cancel availability | Scheduling diagnostics |
| Queued or sending | Progress, waiting count/age, failures needing attention, latest refresh | Throughput, duration, worker details |
| Completed | Delivered, failed/bounced, unique clicks, optional tracked opens; all drill down to recipients | Detailed rate definitions, timestamps, raw events |

Define counts before changing their labels. The demo sent campaign shows “Sent 3” and “Delivered 4”: current recipient status and historical delivery are different dimensions. Explain them or replace “Sent” with a clearly defined metric. Do not force these counters into an additive funnel when their populations overlap. Label tracked opens as imperfect engagement evidence.

### Contacts and audiences

Contacts should open to a manageable first page or clearly named saved views such as Recent, Cannot receive marketing, and Needs verification. Email search should be the dominant control; advanced filtering stays available. Show applied filters with individual removal links and preserve filters/pagination when returning from a record.

Audience defaults to a searchable member list beneath a compact summary: total members, eligible for this audience/client, excluded, and definition of inactivity. Provide visible Members, Segments, Campaigns, and Activity navigation. Do not duplicate the entire contact explorer interface and all history on one page.

Contact detail should answer: “Can this person receive this kind of email from this integration, and why?” Keep campaign eligibility distinct from transactional eligibility and from consent. Use one explanation, then a recent-message list. Place routine subscription/tag edits in a focused edit flow; separate global suppression changes and explain their effects. Retain the existing full eligibility breakdown for people who need it.

### Email activity

Search by recipient; filter by status, period, campaign/template, and message type. Rows show recipient, subject, state, latest event, and a readable issue. Record detail leads with outcome and timeline, then preview and safe recovery actions; provider identifiers and callback payloads remain available under diagnostics.

As a first release, searchable transactional history is more valuable than implementing a complex unified event model. A combined activity view can follow once campaign and transactional state definitions are consistent.

### Inbox

Use recognizable mailbox navigation: Unread, All mail, Blocked. Keep sender and subject together with a snippet and time. Make the subject a link, preserve the list filter when going back, and consider a next-message action.

Source inspection shows message details mark received messages read on GET, but no visible mark-unread action. Add a deliberate read/unread control if revisiting messages is part of the job. Put the body before raw authentication metadata. Explain suspected spam in plain language and place SPF/DKIM/DMARC results in details.

Domain blocking needs a specific scope explanation because it affects every sender at that domain. Example: “Block all future mail from example.com? Messages from every address at this domain will be discarded.” Preserve the separate single-sender action. Do not promise recovery of discarded mail.

### Integration setup and documentation

Use a readiness checklist: sender configured, active key available, template ready where needed, test completed. Link each unmet item to its exact setting. Do not treat a configured value as proof that mail can be delivered.

Sender fields should be address, optional display name, integration identifier, and default choice, with line-format input as an optional advanced import. Show key purpose and last use prominently, preserve one-time secret display, and distinguish integration keys from service admin keys.

Developer docs start with “Send a test email” or “Submit a task”, followed by required setup and expected result. Keep examples copyable and use the configured environment's base URL. This repo currently has only a sandbox deployment; do not label it production.

## Visual and accessibility requirements

The current typography, restrained color palette, and plain Django layout provide a usable foundation. Preserve them while changing hierarchy.

- Use one obvious primary action per state and consistent labels. Keep frequent work visible; disclose infrequent diagnostics. This follows the task-focused simplicity and recognition principles in [Nielsen's usability heuristics](https://www.nngroup.com/articles/ten-usability-heuristics/).
- Use local navigation for distinct tasks, and a single disclosure for secondary details. Do not hide blocking reasons or current scope. This applies [progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/) to optional complexity.
- Reduce repeated title/identity grids, zero counters, empty sections, and explanatory paragraphs that restate headings. Maintain readable rows and clear relationships rather than merely shrinking fonts.
- On phones, keep integration context visible when the sidebar is collapsed. Label its trigger “Menu”. Prefer responsive triage rows; preserve scrolling tables for genuinely two-dimensional data. [WCAG reflow guidance](https://www.w3.org/WAI/WCAG22/Understanding/reflow.html) permits exceptions for such tables, so horizontal table scrolling alone is not a conformance failure.
- Fix unique form IDs and programmatic labels. Associate help and errors with controls, add a linked error summary, preserve submitted values, and place focus at actionable feedback.
- Add a skip link. Ensure keyboard access to navigation, filters, previews, and disclosures, and meaningful current-page indication on message details. Keep status understandable without color or hover-only explanations.
- Measure contrast in both themes. Validate zoom and narrow widths, visible focus, and targets meeting the [WCAG 2.2 minimum-size criterion and exceptions](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html). Check sticky UI against [focus visibility requirements](https://www.w3.org/WAI/WCAG22/Understanding/focus-not-obscured-minimum). These are verification requirements, not claims that current contrast or target sizes fail.

## Delivery sequence and acceptance criteria

### First pass: correct states and remove avoidable friction

Own `templates/base.html`, audience/contact presentation, affected forms, client-selection routing, and relevant services.

- UX01: an unverified or blocked audience member never receives a green eligibility indicator; list and detail agree for the same scope.
- UX02: the interface makes no delayed-send promise unless dispatch is verified to respect it. Tests cover future time, timezone interpretation, and cancellation if scheduling is implemented.
- UX03: switching integration from list, detail, edit, and browser Back yields a valid destination with explicit context; test two browser tabs and unsaved input.
- UX10–11: audience controls have observable effects; every form control has a unique ID and correct label.
- UX16–17: failed jobs have an obvious route; empty states offer real actions; product and state names are consistent.

Use focused behavior tests for scope, eligibility, and timing. UI checks should verify task outcomes, not only matching text.

### Second pass: prioritize daily work

Own dashboard layout, audience members view, contact presentation, and shared CSS.

- At 1440 × 900, actionable issues or an explicit all-clear appear before inventory and diagnostics.
- Audience members and their effective status are visible without scrolling through segmentation reports.
- At 390px, a user can identify a person, understand eligibility, and open the issue without sideways scrolling.
- Inactive, unsubscribed, suppressed, and failed states are clearly different and explain what action is appropriate.
- Filters and list position survive a detail-page round trip. Test populated, empty, filtered-empty, and unavailable-data states.

### Third pass: complete preparation and investigation workflows

Own campaign form/review/detail, message activity routes and queries, previews, integration settings, and API documentation navigation.

- An operator can save an incomplete campaign, resume it, inspect recipients and rendered content, send a restricted test, and review everything before submission.
- An operator can find a historical transactional message without knowing its template or internal ID.
- State and count definitions remain consistent between overview, list, detail, and filtered results.
- Integration setup leads to a verified test result. Optional integrations and diagnostics do not obstruct basic setup.

Reuse existing preview, test-send, and recount services after checking their contracts. Broader activity search, genuine scheduling, and draft relaxation require backend changes; they cannot be delivered as template changes alone.

## Validate with actual users

Run moderated sessions with roughly five to eight people spanning campaign, support, and integration tasks. Start with the current interface to establish a baseline. Have people perform tasks without telling them navigation labels.

| Task prompt | What to observe | Proposed success criterion |
| --- | --- | --- |
| Prepare an announcement for verified subscribers with a specified tag | Choice of audience, interpretation of tags, exclusion awareness, content review | Correct recipients and timing; no send before review |
| Explain why a named learner did not receive confirmation | Search path, distinction between queued/failed/delivered, next action | Correct explanation and safe recovery within two minutes |
| Determine whether an unverified subscriber can receive this campaign | Whether green subscription status misleads | Correct answer without opening hidden diagnostics |
| Switch integrations while investigating a record | Context awareness, Back behavior, cross-tab assumptions | No 404, lost draft, or unintended action under another integration |
| Configure a sender and find the appropriate API key | Setup order, technical syntax burden, key-scope understanding | Complete setup without editing raw structured text |
| Review a mailbox message and block one sender | Whether domain block is confused with address block | Intended scope selected; consequence understood |

The time target above is a proposed benchmark, not a measured improvement. Also record completion rate, wrong-context actions, navigation detours, help requests, and confidence after each task. Prefer task outcomes over total click counts.

Prioritize interviews about frequency: how often campaigns are prepared here versus imported, who investigates transactional failures, whether mailbox operators use mobile, and who owns jobs/schedules. Those answers determine the default home content and the order of larger investments.

## Evidence and implementation entry points

| Evidence | Source |
| --- | --- |
| Dashboard counters and issue placement | [Desktop capture](assets/2026-10-02/dashboard-1440.png), [dashboard template](../../templates/mailing/dashboard.html) |
| Audience density and subscription-as-sendability | [Desktop capture](assets/2026-10-02/audience-1440.png), [audience template](../../templates/mailing/operator/audience_detail.html), [presentation service](../../mailing/services/operator_ui.py) |
| Campaign preparation | [Desktop capture](assets/2026-10-02/campaign-new-1440.png), [campaign form](../../templates/mailing/operator/campaign_form.html), [forms](../../mailing/forms.py) |
| Client switch returns 404 | [Browser capture](assets/2026-10-02/client-switch-404.png), [base template](../../templates/base.html), [client selection and detail views](../../mailing/views.py) |
| Mobile results obscure issue and activity | [390px capture](assets/2026-10-02/contact-results-390.png), [contact search](../../templates/mailing/operator/contact_search.html), [CSS](../../static/mailing/css/app.css) |
| Template preview placement and rendering | [Desktop capture](assets/2026-10-02/template-1440.png), [template detail](../../templates/mailing/operator/template_detail.html) |
| Schedule semantics | [Queue service](../../mailing/services/campaigns.py), [sender](../../mailing/services/campaign_sender.py), [campaign detail](../../templates/mailing/operator/campaign_detail.html) |
| Global mailbox and missing job navigation | [Inbound views](../../mailing/views.py), [inbound detail](../../templates/mailing/operator/inbound_message_detail.html), [job routes](../../jobs/urls.py), [job views](../../jobs/views.py) |

Implementation links above are relative to `docs/audits`; source files are at the repository root.
