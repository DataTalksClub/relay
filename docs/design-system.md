# Datamailer Design System

Datamailer is an operational email tool. The interface should be clean, easy to scan, and calm under production pressure.

We build on dakit, the shared design system for the internal tools
(`../dakit`; its generated bundle is vendored in `static/dakit/` — run
`make sync-dakit` after changing dakit, then commit both repos). Keep the
app itself plain Django templates and CSS: do not add Bootstrap, Tailwind, React, a SPA framework, or a required frontend build pipeline unless a future issue explicitly changes this decision.

## References

Use these references for UI elements and interaction quality:

- dakit (`../dakit`, vendored in `static/dakit/`): the primary reference.
  It is the shared token set and component vocabulary extracted from the
  DataOps look — GitHub Primer neutrals, restrained borders, compact
  row-based content, and the CMP blue accent. App styles use its semantic
  `--dk-*` tokens only, in both themes.
- GitHub Primer: primary reference for product UI foundations, compact navigation, subdued surfaces, tables, forms, labels, focus states, and status treatment.
- Shopify Polaris: secondary reference for admin workflow discipline, especially promoted filters vs advanced filters and clear page actions.
- Resend: secondary reference for API docs, API keys, developer-facing settings, sparse examples, and transactional email vocabulary.
- Dapier ("Operator's Ledger") is a sibling look, not the reference here; keep
  Datamailer aligned with dakit.

Use Postmark only for product concepts, not visual styling:

- delivery activity
- bounces and complaints
- suppression handling
- message streams
- transactional vs broadcast email operations

## Principles

- Clean and easy first.
- Operational clarity over marketing polish.
- Dense but readable beats decorative and oversized.
- The first screen of a detail page must answer the core operational question.
- Primary state and primary action should be obvious.
- Raw provider payloads, audit metadata, and debugging details belong in secondary sections.
- UI labels and URLs should use user language, not implementation language.
- No backwards compatibility is required before production unless an issue explicitly says otherwise.

Core questions by page:

- Contact: can we email this person, and why or why not?
- Campaign: what was sent, to whom, and what happened?
- Audience: who is included, how are they segmented, and who is inactive?
- Client: which integration is this, which API keys are active, and what are they used for?
- Template: what context is required, and what will be rendered?
- API docs: how do I make the request, and what response should I expect?

## CSS Direction

Keep CSS small and explicit:

- design tokens come from dakit's semantic `--dk-*` roles (both themes);
  `app.css` defines no tokens of its own
- reusable layout primitives
- reusable form/table/badge/button/disclosure patterns
- page-specific CSS only when a reusable primitive is not enough

The default implementation should work with plain Django templates and static CSS.

Preferred file direction:

- `static/mailing/css/app.css` for the product UI on top of dakit
- no large inline CSS in templates
- the vendored dakit bundle (`static/dakit/`) is the only generated CSS
  checked in; it is a dependency snapshot, not app build output — refresh
  it with `make sync-dakit` and commit alongside the dakit change

Current shared primitive hooks:

- shell/navigation: `.app-header`, `.app-nav`, `.app-shell`, `.app-sidebar`, `.sidebar-nav`, `.sidebar-toggle`, `.app-main`, `aria-current="page"`
- layout: `.page-header`, `.section-header`, `.section`, `.detail-section`, `.breadcrumbs`, `.meta-grid`, `.stats-grid`, `.detail-grid`, `.compact-list`, `.compact-row`
- actions: `.button`, `.button.secondary`, `.button.danger`, `.actions`, `.action-row`, `.toolbar`
- forms/filters: `.form-grid`, `.form-field`, `.field-errors`, `.form-errors`, `.helptext`, `.filter-grid`, `.promoted-filter-grid`, `.filter-bar`, `.checkbox-list`, `.advanced-panel`
- operational data: `.table-wrap`, `.nowrap`, `.truncate`, `.pagination`, `.empty-state`
- state and feedback: `.badge`, `.badge.success`, `.badge.warning`, `.badge.danger`, `.badge.neutral`, `.messages`, `.message`, `.alert`
- activity/debug: `.timeline`, `.timeline-item`, `.audit-row`, `pre`, `code`

## Tokens

All reusable styling must flow through dakit's semantic tokens before page-specific CSS is added. If a style needs a color dakit doesn't have, add the role in dakit (light and dark values, contrast pair, rebuild) rather than hardcoding it — that is how `--dk-danger-hover` was added for relay's solid danger buttons.

### Color Tokens

App styles use dakit's semantic `--dk-*` roles only — never the primitive
ramps (`--dk-gray-*`, `--dk-blue-*`, …) and never raw hex values. dakit's
build verifies AA contrast for every text/background pair in both themes.

Surfaces and text:

- `--dk-bg-page` (canvas), `--dk-bg-muted` (sidebar and secondary
  surfaces), `--dk-bg-hover` (hover/active tone)
- `--dk-text-primary`, `--dk-text-heading`, `--dk-text-muted`,
  `--dk-text-faint` — the text hierarchy
- `--dk-border-default`, `--dk-border-strong`

Actions:

- `--dk-accent-default` / `--dk-accent-hover` — filled controls (dark mode
  flips to a lighter fill with dark on-accent text)
- `--dk-text-on-accent` — foreground on filled controls
- `--dk-text-link` / `--dk-text-link-hover` — links and selected-nav text
- `--dk-accent-soft` — selected navigation and focus wash
- `--dk-focus-ring` — keyboard focus outline
- `--dk-danger-hover` — hover fill of solid danger buttons

State: the `--dk-{success,warning,danger,info}-{text,bg,border}`
triplets. Neutral badges use `--dk-text-muted` on `--dk-bg-muted` with
`--dk-border-default`.

### Spacing, Shape, And Type Tokens

- `--dk-space-1…8` — the 4px grid; relay layouts on the old 32px step use
  `--dk-space-7`
- `--dk-radius-sm` (4px: inputs, nav items, small inset surfaces) and
  `--dk-radius-md` (6px: buttons, panels, empty states, table wrappers)
- `--dk-text-sm` / `--dk-text-body` / `--dk-text-lg` / `--dk-text-page`
  and `--dk-font-sans` / `--dk-font-mono`
- `--dk-size-control-md` (34px inputs and buttons),
  `--dk-size-content-max` (main column), `--dk-size-sidebar`

Letter spacing stays normal. Font sizes do not scale with viewport width.
Inputs and other inset surfaces use the 4px radius step; buttons and
panels use the 6px step. Normal surfaces carry no shadow; shadows
are reserved for overlays.

## Component Contract

Use these primitives before creating page-specific classes.

### App Shell

- Top header contains global actions: brand, Clients, API Docs, Admin, active client switcher.
- Left sidebar contains active-client navigation: Dashboard, Campaigns, Audiences, Contacts, Templates, Client settings.
- Sidebar can be collapsed and must remain keyboard accessible.
- Do not repeat active client text inside every scoped page unless the user needs it to disambiguate a destructive action.

### Filters

- Use promoted filters for the 2-4 controls most users need first.
- Put secondary filters in `.advanced-panel`.
- Active filters appear as badges below the form.
- Avoid showing large checkbox groups by default.

### Tables

- Default list tables should fit 4-6 meaningful columns.
- Combine related status details into one cell using badges and muted secondary text.
- Move raw IDs, provider IDs, metadata, and error payloads into detail pages or disclosures.

### Details And Diagnostics

- Summary sections answer the operational question first.
- Diagnostics use `<details>` or a secondary section.
- Raw context, rendered HTML, SES IDs, metadata, and audit rows are diagnostics unless the page is explicitly a debugging page.

## Layout

- Use a stable app shell with clear navigation.
- Prefer compact top navigation or a restrained sidebar if navigation grows.
- Use active navigation states.
- Use breadcrumbs on detail pages when they improve orientation.
- Keep content widths readable, but allow operational tables to use wider space.
- Avoid nested cards.
- Use sections, tables, split layouts, and timelines instead of card grids.
- On mobile, prioritize core state and actions before large tables.

## Typography

- Use dakit's self-hosted Inter stack (`--dk-font-sans`) for UI text and IBM
  Plex Mono (`--dk-font-mono`) for quantities, timings, IDs, and code. dakit
  ships both fonts from `static/dakit/fonts/` (SIL OFL); the app declares no
  `@font-face` rules of its own, and fonts must not make third-party requests.
- Keep letter spacing normal.
- Do not scale font size with viewport width.
- Page titles are 32px semibold on desktop and 22px on mobile.
- Section headings are compact (16px semibold).
- Body and row text is 14px; table and metadata text may drop to 12-13px.
- Help text should be short and muted.

## Color

Use a restrained neutral base:

- strong text
- muted text
- border
- subtle background
- elevated surface only when needed

Use status colors consistently:

- success: verified, delivered, active, subscribed
- warning: pending, risky, skipped, needs attention
- danger: bounced, complained, failed, unsubscribed, revoked
- neutral: draft, unknown, inactive, none

Do not let pages become dominated by one hue or by decorative gradients.

## Components

### Buttons

- One primary action per page region.
- Secondary actions should be visually quieter.
- Destructive actions use danger styling and confirmation.
- Avoid button rows with many equally loud actions.

### Badges

Use badges for short state labels:

- verified / unverified
- valid / risky / invalid
- subscribed / unsubscribed
- active / revoked
- delivered / bounced / complained / failed

Badges should not wrap awkwardly inside tables.

### Tables

Tables are the default for operational lists.

- Keep columns predictable.
- Use truncation for long email, URL, metadata, and subject values.
- Avoid cramming too many columns into the first view.
- Move secondary data into detail pages or expandable rows.
- Preserve filters and pagination state.
- Empty states should say what is missing and what the next action is.

### Forms

- Every input has a visible label.
- Related fields are grouped.
- Validation errors appear next to the relevant field.
- Long text areas need context or preview when the user is composing something important.
- Mutation-heavy forms should not dominate read-only detail pages.

### Detail Pages

Use a summary-first structure:

1. Identity and state.
2. Sendability or operational eligibility.
3. Key metrics.
4. Membership/configuration.
5. Recent activity.
6. Audit/debug details.

If a page becomes long, split it into clear sections or tabs. Do not place every edit form at the top.

### API Docs

API docs should feel closer to Resend than to generated reference docs:

- examples before exhaustive reference
- copy-pasteable curl examples
- demo API keys from local seed data
- example request bodies
- example success responses
- common error responses
- links from examples to API key management

## URL And IA Rules

- Product UI routes should not use `/operator`.
- Client API routes should use `/api` before production.
- Contact UI URLs should use stored normalized email addresses instead of numeric IDs.
- Keep Django admin at `/admin/`.
- Route names, page titles, and navigation labels should use the same vocabulary.

## Accessibility

- Inputs must have labels.
- Links and buttons must be keyboard reachable.
- Focus states must be visible.
- Text must not overlap or overflow at mobile widths.
- Tables need accessible headers.
- Public unsubscribe pages must be simple, accessible, and trustworthy.

## Applying This Guide

All UI issues should reference this guide. When changing a page, check:

- Does the first screen answer the page's core question?
- Are actions ranked by importance?
- Are tables readable and not overloaded?
- Are statuses consistent with the shared badge language?
- Is the URL human-readable?
- Would the page still be usable during an incident or production email send?
