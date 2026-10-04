# Family attack — Relay operator UI (round 9, 2026-10-03)

Adversarial audit of the served operator UI (all major pages screenshotted at
1440×900 and 390×844, light + dark, from the live tree; evidence under
`/tmp/relay-shots/out-before-hs/`), checked against the dakit family spec
(`../dakit/docs/family.md`), the dataops design system
(`../dataops/frontend/DESIGN_SYSTEM.md`), the dakit showcase, and dataops's
rendered Home. Each claim is concrete and re-verifiable.

## Generic AI-template tells

- **A1 — Stat-box metric rows.** Campaign draft "Recipient review" renders
  Candidates / Filtered by tags / Eligible / Skipped as four equal bordered
  boxes; contact detail "Engagement and Delivery" renders six equal boxes,
  five of them "No data". The family answer is one segmented strip
  (dataops Home: Overdue / Due today / Follow-ups due / Missing proof).
- **A2 — Quick Links card grid.** Overview ends in a 3-equal-card grid
  (Audiences / Contacts / Client settings) duplicating sidebar destinations:
  dashboard filler, not operator hierarchy.
- **A3 — Explainer stacking.** A muted sub-line sits under nearly every
  heading ("Scan current state, audience, send timing, and delivery
  signals."; "Counts reflect current subscriptions and sending policy…"),
  and Background processing carries two stacked explainers plus a third
  qualifier sentence.
- **A4 — Disclosure box stack.** Contact detail renders five consecutive
  full-width bordered `<details>` boxes (Global verification…, Full send
  eligibility…, Campaign history, Transactional Messages, Full event
  timeline…); Overview adds two more. Boxes instead of rhythm.
- **A5 — Pills everywhere.** `.badge` hardcodes `border-radius: 999px`
  (app.css) with tinted fills; every list row carries one (email activity
  shows nine distinct pill statuses; draft sample rows show a green
  "Eligible" pill each). dakit badges are radius-sm 11px chips.
- **A6 — Primary-action inflation.** Contact detail shows three filled blue
  primaries (Update subscription / Add tag / Remove tag) because every bare
  `<button>` is styled as primary. Client detail renders "Revoke key" as a
  large filled red button inside the table row. Family: one primary per
  view; destructive actions quiet, labelled, separated.
- **A7 — Duplicated badges.** Client header: "Current client" chip + "Active
  client" + "1 active API keys" pills while the sidebar already shows the
  active client; campaign draft header: "Draft" + "Not sent".
- **A8 — Rows as cards.** Contact-detail Recent Activity renders each event
  as its own bordered rounded card with gaps; mobile campaigns renders each
  campaign as its own card. Family: one container, divided rows,
  edge-to-edge on phones.

## Deviations from the dakit/dataops family

- **F1 — No icon language.** Zero `<svg>` elements in any template; nav is
  text-only. The family mandates the shared 24-grid set (20px, stroke 1.8,
  round caps) with icon-plus-text nav rows — dataops's sidebar shows the
  pattern this app must match.
- **F2 — Stale vendored dakit.** `static/dakit/dist/dakit.css` predates the
  current bundle: no `.dk-form-actions`, 2px focus ring instead of 3px + 2px
  offset, no text-control focus halo, missing control hover border. Relay's
  own `input:focus` halo uses `--dk-accent-soft` instead of
  `--dk-focus-ring`.
- **F3 — Sidebar recipe gaps.** Selection style is close (accent-soft fill,
  accent text, no stripe) but rows use radius-sm (recipe: radius-md), carry
  no icons, and the client switcher renders as a boxed card inside the nav
  column.
- **F4 — Header chrome.** Bordered box around the "Dark" theme toggle; text
  links loose in the toolbar with no icon treatment or identity area.
- **F5 — Button drift.** Two competing `.button` definitions (radius-sm vs
  radius-md); padding/height off the dakit recipe; secondary hover uses
  `--dk-border-strong` instead of `--dk-border-control-hover`; danger is
  filled instead of quiet outline.
- **F6 — Form language.** Colon-suffixed labels ("Audience:", "Key name:",
  "Name:"); helper text placed inconsistently (some above/next to fields);
  no `.dk-form-actions` footer; three same-weight forms side by side on the
  contact page, each with its own primary.
- **F7 — Status colors bypass the checked triplets.** `.badge` pairs
  `--dk-text-muted` with muted/tinted fills instead of the triplet `*-text`
  roles dakit WCAG-checks at build time.
- **F8 — Raw hex.** `.email-preview-frame { background: #fff }` — stays
  white in dark mode (dark-mode defect) and breaks the no-local-hex rule.
- **F9 — Table overflow.** The campaigns table clips its last column at
  1440px ("Review before send:" is cut off): no working scroll/wrap
  strategy.
- **F10 — Mono misuse.** Sentence-length delivery summaries render in mono
  ("3 currently sent / 4 delivery confirmations"), diluting mono's meaning
  (quantities, timings, IDs).
- **F11 — Unstyled local nav.** Client detail's section links (Setup
  checklist / Senders / API keys / Integrations / Diagnostics) render as a
  bare text row with no component treatment.

## Direction

No new direction is invented: the contract is the family contract, restated
for Relay in `docs/design-contract.md` with a banned list. The re-skin keeps
every class name, URL, and server/JS-emitted markup working.
