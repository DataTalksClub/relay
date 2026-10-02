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
7. **Icons** — inline SVG on the family 24 grid (viewBox `0 0 24 24`) rendered
   at 20px, `stroke="currentColor"`, stroke-width 1.8, round caps/joins, no
   fills; no emoji or glyph icons anywhere; the retired 16x16/stroke-1.5 set
   must not appear alongside it.
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
| 7 | Icons | Pass (re-reviewed against the 2026-10-02 24-grid spec) | The original review measured the then-current 16x16/stroke-1.5 set. That geometry was retired by the settled family spec (dakit `docs/family.md` §Icons, 71a39d3); the chrome icons have since been migrated to the family 24 grid — viewBox `0 0 24 24`, rendered 20px, stroke-width 1.8, round caps/joins, no fills, original lucide 24-grid path data (filter→funnel, code-2→code-xml aliases), semantics unchanged. No mixed sets: grep finds no `0 0 16 16` / stroke-width 1.5 icons left in templates. |
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

## Independent judge rounds (2026-10-02, evening)

The self-review above was later checked by independent visual judges — fresh
subagents shown only the dakit family-reference screenshots and full-page
captures of every operator surface (14 pages + drawer, 1440x900 and 390x844,
light and dark). Evidence lives in `/tmp/relay-family-judge/`
(`judge-round1.md`, `judge-round3.md`, `judge-round4.md` under
`shots-roundN/`; rounds 5–6 under this worktree's `tmp-family-judge-r5/`
and `tmp-family-judge-r6/`).

- **Round 1 — FAIL (14/15 surfaces FAMILY-BREAK).** The shell itself was the
  gap: navigation pills in the toolbar, a bare "Menu" phone bar, free-floating
  section headings over loose tables, card grids instead of segmented strips,
  missing stat dots and empty-state pills. Fixes: `05c4e5d` (bundle refresh),
  `5201a86` (global nav moved into the sidebar, quiet icon toolbar),
  `9016a80` (page title in the phone top bar).
- **Round 2 — PASS at `5912faf`** (zero blocking findings; the judge
  recommended re-judging since `12983d7`/`3bd10ad` were about to touch
  the same templates).
- **Round 3 — FAIL on the content layer and mobile; shell now passes.** The
  judge confirmed the shell frame ("the family gap is now in the content layer
  and on mobile, not in the shell frame") and flagged: broken mobile
  table-to-row conversion (no padding, mixed label fonts), underlined accent
  row links, strips without visible hairlines (cell backgrounds painted over
  the pulled borders) and a dark wrong-surface strip, unpadded filter panels,
  an empty badge cell, redundant decorative pills, ragged per-row actions, and
  tinted code blocks on api-docs. Fixes: `7d7dcb3` (dashboard panels, stat
  dots), `8abe727` (list pages into banded panels with count meta and empty
  pills), `b73b23f` (detail pages, forms, and API docs into the panel pattern;
  segmented stage control), plus `12983d7`/`3bd10ad` (gap-technique strip
  hairlines on the surface tint, ink row links with hover underline, bordered
  mobile record cards with label/value lines, content-width phone controls,
  pill discipline, em-dash empty markers, drawer close control).
- **Round 4 — FAIL on one blocking item (B-1)**: campaign-detail summary
  and client-detail identity meta rendered as loose stat boxes instead of
  single segmented-strip containers, plus eight polish findings (stacked
  badge stretching, label colons, group-label letter-spacing, underlined
  row links, mobile record padding, empty badge cells, floating inline
  form actions; the missing global search stayed a documented deviation).
- **Round 5 — PASS**: B-1 and every polish item verified fixed on the
  surfaces they named; two new findings were refinement-level (strip
  hairline fidelity, one width inconsistency) with no recipe violations.
- **Round 6 — PASS, final fresh-judge verification** (evidence:
  `tmp-family-judge-r6/VERDICT-round6-judge-cc6affd.md`): all ten rubric
  criteria hold in both themes at both viewports against the dataops
  references, with B-1 and the polish items confirmed fixed from direct
  visual evidence. One non-blocking follow-up recorded (P-9: ~20
  hardcoded `font-size` literals instead of `--dk-text-*` tokens — the
  dataops reference shares this habit; re-point them in a later
  token-discipline pass).
- **Round 7 — PASS, the icon-geometry migration** (evidence:
  `tmp-family-judge-r7-icons/`, 34 renders, both themes, 1440 and 390):
  dakit `71a39d3` retired the 16-box/stroke-1.5 set, so all twenty
  sidebar, toolbar, theme, and action glyphs moved to the 24 grid at
  20px, stroke 1.8 (`d6fa542`), and the vendored bundle was refreshed to
  the family focus recipe (`83e536c`, dakit `dea377c`). The judge read
  every render at one family weight with no icon-bump layout regressions
  and genuinely dark darks; the only finding (campaign-detail's
  recipients table scrolling its clipped "Assume sent" button) is
  pre-existing, reachable by scroll, and not user-rejecting.

Tests stayed green throughout (721 passed after each fix batch, and
again after the icon migration).

## Result

**Accepted by the owner's standard after independent judging.** Side-by-side
with the family reference, the operator UI shares the shell geometry, sidebar
recipe, page-header scale, banded panel rhythm, segmented strips, button
hierarchy, status language, icon strokes, and `--dk-*` palette in both themes
at both viewports. All fixes from the judge rounds are covered by tests
(721 passed) and the screenshots listed above.
