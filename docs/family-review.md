# Family review — relay operator UI vs the dakit family spec

Reviewed branch: `redesign/dataops-family` (through the completeness pass that
added the shell-tightening and form-footer commits). Rubric and verdict written
by the finishing agent; no judge subagent tool was available in this session,
so the review was performed directly against the written rubric below, using
rendered screenshots compared side-by-side with `dakit/docs/family-reference/*.png`.

Evidence: 30 screenshots in `tmp-family-shots/` (dashboard, campaign list,
campaign detail, client list, client detail, audience list, campaign form at
1440x900 and 390x844, light and dark, plus the mobile drawer open state),
captured from this worktree with a scratch sqlite database.

## Rubric

1. **Shell geometry** — 268px `--dk-size-sidebar` sidebar + canvas + slim
   toolbar for global actions; mobile <=820px: 64px top bar, nav as modal
   drawer with scrim, Escape, focus move; sidebar collapsible with visible
   restore.
2. **Sidebar recipe** — `--dk-bg-muted` plane, 36px icon+text rows at
   radius-md weight 500/600, accent-soft selected row with no rail/stripe,
   uppercase muted group labels, workspace context above nav, account control
   at the toolbar edge.
3. **Type scale** — one h1 per page at `--dk-text-page` (32px, 22px mobile) +
   one-line muted description; section headings `--dk-text-lg`; mono stat
   values.
4. **Row rhythm** — one bordered container with hairline-divided rows; header
   bands on `--dk-bg-muted`; summary counts as a segmented strip, not card
   grids; tables only for genuinely tabular comparison, converted to labelled
   rows on mobile.
5. **Button hierarchy** — one primary per view (filled accent), secondary =
   muted surface + neutral border, destructive outlined; content-width on
   desktop, full-width on phone; form footer row above a top border with
   primary first, then Cancel.
6. **Controls** — heights from `--dk-size-control-md` (34px), radius-md,
   chevron on selects, token focus ring, labels above controls with hints
   below.
7. **Icons** — inline SVG 16x16, `stroke="currentColor"`, stroke-width 1.5,
   round caps, no fills; no emoji or glyph icons anywhere.
8. **Status language** — badge triplet tones only (success/warning/danger/
   info + neutral), text+bg+border together; no reflex badges.
9. **Dark mode** — `data-theme` on the document element; all values from
   `--dk-*` tokens; no inverted light values; overlays use
   `--dk-bg-backdrop` + `--dk-shadow-overlay`.
10. **No off-token values** — no app-local hex, radii, shadows, or control
    sizes in app.css outside `--dk-*` roles.

## Verdict per criterion

| # | Criterion | Verdict | Notes |
|---|-----------|---------|-------|
| 1 | Shell geometry | Pass | `grid-template-columns: var(--dk-size-sidebar)`; collapsed rail keeps a visible restore control; mobile drawer has scrim (token backdrop), Escape, close on nav click, focus moves into the drawer. |
| 2 | Sidebar recipe | Pass (after completeness pass) | Plane moved from `--dk-bg-page` to `--dk-bg-muted`; rows are now 36px/10px radius-md 500/600; selection is the filled row only. Active-client switcher plays the workspace-context slot; account chip (avatar + name, sign-out) added at the toolbar edge. |
| 3 | Type scale | Pass | h1 32px/1.2 semibold, 22px on phone; muted one-line descriptions; mono stat values. |
| 4 | Row rhythm | Pass (after completeness pass) | Count strips are one segmented container with pulled hairlines; dashboard attention queue is a banded panel with divided rows; campaign/audience/client lists convert to labelled rows on phone via `stack-table` (data-labels added this pass); campaign table auto-sizes so 1440x900 no longer clips the Delivery column. |
| 5 | Button hierarchy | Pass (after completeness pass) | One primary per view; stacked forms now exit through `.form-actions` (top border, primary first, Cancel after) — fixed this pass for campaign/client/audience/tag/receiving-address forms; phone stacks buttons full-width. |
| 6 | Controls | Pass | 34px `--dk-size-control-md`, radius-md (fixed from radius-sm this pass), dakit chevron, dataops focus ring (`box-shadow 0 0 0 3px var(--dk-focus-ring)`) added this pass; labels above, hints below, errors at the field. |
| 7 | Icons | Pass | All icons are inline 16x16 stroke-1.5 round-cap SVGs sharing the dakit chevron language; grep finds no emoji/glyph icons in templates or CSS. |
| 8 | Status language | Pass | `.badge` mirrors `.dk-badge` (radius-sm, xs, medium) with triplet tones; badges appear where state changes a decision (campaign status, key health, worker health). |
| 9 | Dark mode | Pass | Boot script pins `data-theme` before first paint; dark values come from the vendored dakit remap; drawer scrim uses `--dk-bg-backdrop` (raw `rgb(0 0 0 / 0.4)` replaced this pass); overlay shadow is `--dk-shadow-overlay`. |
| 10 | No off-token values | Pass | app.css carries no hex colors (the one exception is the email preview frame, documented as content-not-chrome: emails render on paper in both themes); radii, shadows, and control sizes are all `--dk-*` roles; remaining raw px values are structural layout (rail width, small-type sizes), which the spec's acceptance list does not reserve to tokens. |

## Known deviations (accepted, with reasons)

- **No global search above the sidebar nav.** dataops has one; relay has no
  cross-entity search endpoint — the only search is contact-scoped. Adding a
  search box that silently covers one entity would violate the no-false-
  affordance rule, and inventing a backend search is feature work outside the
  redesign. The workspace-context slot is occupied by the active-client
  switcher instead. Revisit if a global search endpoint lands.
- **Relay lists keep mono digits and slightly denser tables than dataops
  queues** — a content difference (genuinely tabular comparison columns), not
  a pattern difference; mobile converts to labelled rows as prescribed.
- **Email preview frame stays light in dark mode** — sanctioned: it renders
  message content, not app chrome.

## Result

**Accepted.** Side-by-side with the family reference, the operator UI shares
the shell geometry, page-header scale, row rhythm, button hierarchy, status
language, icon strokes, and `--dk-*` palette in both themes. All fixes from
the completeness pass are covered by tests (721 passed) and the screenshots
listed above.
