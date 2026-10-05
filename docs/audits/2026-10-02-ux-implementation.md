# Relay UX implementation — 2 October 2026

This is the first-pass record. See [UX completion](2026-10-02-ux-completion.md) for the subsequent work on the updated interface.

Implemented a first pass of the [operator UX audit](2026-10-02-operator-ux-audit.md) in the local working tree. The design follows three task sequences: recipients → message → review → send; person → eligibility → activity → change; email → outcome → details. This work has not been deployed to the sandbox.

## Changes operators can use

- **Find the right workspace.** Navigation groups daily work, integration settings, and shared service destinations. The selected client stays visible on mobile. Switching clients from a record returns to a usable list. Scoped POST actions carry the client shown in the form and reject a stale tab after another tab changes clients. Failed jobs now has a visible route.
- **Start with work that needs attention.** Overview places incidents and campaigns ahead of inventory and detailed processing diagnostics. Routine policy exclusions no longer compete with failures. Unknown processing health is explicitly unavailable rather than healthy.
- **Find a person immediately.** Contacts loads scoped records without requiring a first search. Eligibility follows actual campaign sending policy, including validation, suppression, consent, and verification. Subscription status remains separately visible. Activity timestamps and filters respect the selected client and campaign audience; tags respect the selected organization.
- **Work with audience members first.** Audience detail opens with members and offers local navigation to health, segments, campaigns, and activity. The ignored client dropdown is replaced by a scope explanation. Contact and audience result tables become cards on narrow screens, and long eligibility labels wrap.
- **Make deliberate contact changes.** Contact detail leads with eligibility and recent activity. Subscription and tag management follows; global contact state is disclosed separately with its wider scope explained. Forms have unique IDs and correctly associated labels. Returning from a contact preserves search context.
- **Prepare and review campaigns.** An unfinished message can be saved as a draft. The shorter form groups recipients, optional tag filters, and message content. Review shows sender, audience, eligible count, and an isolated rendered email before explicit send confirmation. If the count changes, the operator must review it again. The queue service rejects missing subjects, missing content, and future send times before recipient snapshotting, including API callers.
- **Investigate transactional delivery.** Email activity searches by recipient, subject, or template and filters by state and creation date. Detail pages preserve the return search. “Sent to provider” and “Delivered” are distinct; provider identifiers and payloads are secondary. Campaign results remain linked from this destination.
- **Approve template content visually.** Template detail starts with a sandboxed full email preview; variable requirements, source, and developer examples follow. The existing truncated preview API contract is preserved. Empty template states link to an actual creation action.
- **Triage the shared inbox.** Sender, subject, status, and body come first. Authentication, headers, and storage details are disclosed. Marking unread returns to the inbox without immediately marking the message read again. Address and domain blocking explain their service-wide scope, with an explicit checkbox for domain blocking. Blocked messages do not fetch stored body content.

## Browser evidence

Walked through the local seeded application at 1440px and 390px. Additional checks at 320px covered overview, contacts, audience detail, campaign editor/review, contact detail, inbox, and email activity. These eight pages returned 200, had no duplicate IDs or unlabelled visible form controls, and fit the viewport. Primary contact and audience cards had no horizontal overflow. The broader walkthrough reported no JavaScript page errors; switching clients from a campaign detail reached the campaign list.

With the same demo content, audience detail decreased from approximately 4719px to 2207px at desktop width, and campaign creation from 1994px to 1448px. The important change is that members and decisions appear earlier; page length alone does not measure usability.

Screenshots are temporary hosted copies of synthetic demo data and expire after 60 days:

- [Overview](https://d31nukezbn4e3o.cloudfront.net/2026/10/02/f0a813c6f3474ffdb676e4fbc36a12f9-7007bbcefe651334.png)
- [Campaign preparation](https://d31nukezbn4e3o.cloudfront.net/2026/10/02/c1a3cbe5babf4d789dfcd454337f0337-146be63966cf133d.png)
- [Mobile contact results](https://d31nukezbn4e3o.cloudfront.net/2026/10/02/156e1562745448cea0935854005a6686-f83681d5f6e8595c.png)

Full screenshots and DOM evidence are under `.tmp/ux-implementation/`; the original audit evidence is retained separately in `assets/2026-10-02/`.

## Validation

**797 tests passed** in the full regression run (411 seconds). The final activity-scope and inbox files also passed a separate run of **13 tests**, covering the last added regression cases. Focused tests cover policy agreement, activity isolation, draft and queue readiness, client switching and stale forms, filtered return navigation, preview escaping, contact form labels, and inbox state transitions. Existing operator assertions were updated for the changed task flow and copy.

Ruff, Django system checks, migration consistency, and whitespace checks pass. Browser checks used an isolated SQLite database and no sends were initiated.

## Remaining work

This pass does not implement every audit recommendation. Campaign scheduling remains unavailable and is stated explicitly; the UI does not offer test sends. Sender setup, client configuration, and the long API reference still need dedicated workflow redesigns. Template sample variables and preview size controls are unchanged. Email activity covers transactional records and links to campaign results rather than combining both histories. Global suppression edits explain scope but do not yet offer a dedicated before/after confirmation screen. Jobs and schedules need broader operational screens if research confirms that need.

Client selection remains session-based: stale submissions are rejected and switching redirects safely, but old record URLs from browser history can still return 404 after a scope change. Moving client scope into URLs would address that broader navigation behavior.

Validate the assumed roles and task priorities with actual operators. Run observed tasks for campaign preparation, finding a missing email, diagnosing an excluded contact, and inbox triage; compare completion, errors, and backtracking with the original interface. Screen-reader testing, a complete contrast audit, realistic large-data performance checks, and authenticated sandbox verification remain outside this local implementation.
