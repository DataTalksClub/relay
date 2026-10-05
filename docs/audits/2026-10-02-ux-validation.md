# Local UX validation — 2 October 2026

This validates the current audience, email activity, integration setup, and developer documentation screens. It complements the [original audit](2026-10-02-operator-ux-audit.md) and implementation report. Checks used synthetic data, an isolated SQLite database, and headless Chromium. No emails were sent and no deployment was changed.

## Findings and fixes

- **Audience loading:** Members was calculating validation and campaign outcome reports that it did not display. It now requests only scoped tag choices. Health now calculates its twelve counts in one aggregate query rather than calculating shared history and replacing those results. Eligibility remains two SQL count queries, with eligible/excluded subqueries used for pagination.
- **Long email history:** Campaign and template selectors now display up to 100 choices. A valid bookmarked selection is retained even when it falls outside those choices. The screen explains the limit, provides full campaign/template list links, and supports subject/template-name search for older email. Active advanced filters are expanded.
- **Sender errors:** Invalid sender policy is presented as an alert, associated with address-row inputs, and receives focus after submission. The invalid request leaves the stored sender policy unchanged. Optional settings containing errors and selected advanced imports are expanded.
- **Keyboard continuity:** Adding a sender focuses its new ID field; removing one focuses an adjacent row or the Add button. Documentation copy buttons have names identifying the example. Code blocks receive keyboard focus so long lines can be scrolled.
- **Definitions:** Audience Health explicitly scopes inactivity to the selected client's campaign history. Contact-level verification counts are distinguished from audience/client subscription verification, which also affects campaign eligibility.

## Automated behavior and scale checks

The focused validation run passed **77 tests** in 29.18 seconds. It includes existing setup/API documentation/operator management regressions and new tests for policy agreement, query bounds, pagination, retained filter selections, and invalid sender saves.

| Synthetic workload | Result | Local measurement |
| --- | --- | --- |
| Audience: 25 versus 1,000 subscribed members | Both load 25 member rows and execute 12 queries; large paginator reports 1,000 | 0.064s versus 0.039s |
| Eligibility: 1,000 members | Two count queries; returns querysets rather than member ID lists | Exact query-count assertion |
| Health: 1,000 members | Twelve counts in one query; no member rows materialized | Exact query-count assertion |
| Tag choices: 1,000 members | One query, scoped to the selected client and audience | Exact query-count assertion |
| Activity: 25 versus 2,000 transactional messages | Both execute ten request queries and load 25 rows; second page retains scope | Large request: 0.045s |
| Selector inventory: 150 campaigns and 150 templates | Initial selectors each contain 100 choices; old bookmarked selections remain available | Behavioral assertions |

Times are single local observations, not latency budgets or a statistical benchmark. Warm caches and interpreter timing can make the larger fixture faster. The useful regression guarantees are bounded rows, stable query counts, correct pagination totals, and scope isolation.

## Browser and accessibility evidence

Thirteen pages/states were checked at **1440px, 390px, and 320px**: all five audience sections, email activity and its empty search, client settings, sender editing, documentation quickstart/workflow, and reference search/empty results. All **39 checks** returned 200, fit the document viewport, and had no duplicate IDs, unlabelled visible form controls, missing described-by targets, or JavaScript page errors.

Keyboard checks verified Skip to content reaches the main region, audience navigation changes the active section, legacy audience/documentation hashes reach the corresponding new screen, address-row addition/removal retains useful focus, and invalid sender submission focuses its explanatory alert. A copy button copied the dry-run example. The narrow code block was focusable for horizontal keyboard scrolling.

Computed text contrast was sampled on nine pages in both light and dark themes: **1,920 visible text checks** found no failures against the 4.5:1 normal-text / 3:1 large-text thresholds after theme transitions finished. Repeated navigation contributes multiple samples. This calculation includes ancestor backgrounds and alpha blending; it is not an accessibility certification or a complete check of gradients, images, hover states, focus indicators, and native controls.

Local evidence is retained under `.tmp/ux-validation/`: `browser-evidence.json`, `accessibility.json`, browser/contrast logs, and `focused-tests.xml`. These are development artifacts rather than permanent published evidence.

## Database considerations

No speculative indexes were added. Existing contact/audience/client subscription uniqueness and foreign-key indexes support the correlated membership predicates, while existing transactional client/status/created and recipient contact/sent indexes support parts of the activity filtering. The current latest-activity expression spans several timestamps and the combined feed still needs sorting; a constant number of queries does not make that sort constant-time.

Before tuning for a substantially larger sandbox dataset, run PostgreSQL `EXPLAIN (ANALYZE, BUFFERS)` on the actual member and activity queries at representative cardinalities. Candidate changes to evaluate include a partial client/delivered-time index for the setup checklist's latest delivered-message lookup, and an indexed maintained activity timestamp if combined-feed sorting becomes a demonstrated bottleneck. Either requires a migration and verification against event updates and backfills. SQLite timing here does not establish PostgreSQL query-plan behavior.

## Honest limits

This work did not involve actual operators, user interviews, screen-reader sessions, real email-client rendering, provider identity verification, or authenticated sandbox traffic. It cannot establish that the inferred jobs to be done match people's priorities, that a delivered email reached an inbox, or that every assistive-technology interaction works.

The next human validation should observe campaign preparation, finding a missing email, explaining an excluded member, configuring a sender/key, and inbox triage. Record completion, mistakes, backtracking, and recovery. Test with a screen reader and keyboard on the actual supported browsers, and measure the real sandbox database before making scale claims. Those require participants and access that local synthetic tests cannot substitute for.
