# Relay design contract — the dataops family

Relay's UI belongs to the **dataops design family**. The sources of truth are
the vendored dakit bundle (`static/dakit/`, synced from `../dakit` via
`scripts/sync_dakit.sh`), the family spec (`../dakit/docs/family.md`), and the
canonical implementation (`../dataops`, see `frontend/DESIGN_SYSTEM.md`).
When this file and a template disagree, the template changes; when dakit and
this file disagree, dakit wins.

## Type

| Role | Value | Token |
| --- | --- | --- |
| Page title | 32px desktop / 22px mobile, semibold, -0.02em | `--dk-text-page` / `--dk-text-xl` |
| Section heading | 16px semibold | `--dk-text-lg` |
| Body, row titles | 14px | `--dk-text-body` |
| Controls, tables | 13px | `--dk-text-md` |
| Labels, hints | 12px | `--dk-text-sm` |
| Chips, badges | 11px | `--dk-text-xs` |
| Quantities, timings, IDs | mono | `--dk-font-mono` |

Mono is for quantities, timings, and identifiers only — never for prose.

## Color

Every color is a `--dk-*` role. Zero app-local hex. Status uses the triplet
roles (`--dk-success-*`, `--dk-warning-*`, `--dk-danger-*`, `--dk-info-*`)
with text + background + border together; the triplets are WCAG-checked at
dakit build time and are never re-mixed from ramps. Dark mode comes from the
tokens (`data-theme` + pinned `color-scheme`); nothing inverts light values.

## Shape, borders, shadow

- Shared component radius is `--dk-radius-md` (6px); small controls and chips
  use `--dk-radius-sm`; overlay panels use `--dk-radius-lg` (10px). Nothing
  else — in particular no pills (999px) and no 0/2px one-offs.
- Normal surfaces carry borders, never shadows. Shadows are overlays/modals
  only (`--dk-shadow-overlay`).
- Focus is the family ring: `outline: 3px solid var(--dk-focus-ring)` with
  `outline-offset: 2px` on `:focus-visible`; text controls additionally gain
  the accent border + `0 0 0 3px var(--dk-focus-ring)` halo on `:focus`.

## Components

- **Buttons.** One primary per view (accent fill, `--dk-text-on-accent`).
  Secondary = muted surface + neutral border + primary text, hover border
  `--dk-border-control-hover`. Danger = quiet outline (`--dk-danger-text` on
  transparent, `--dk-danger-bg` hover), reserved for genuinely destructive
  actions, never a filled red block in a table row. A bare `<button>` element
  is styled secondary, not primary. Heights from the `--dk-size-control-*`
  scale (34px desktop, 44px touch on phones); content-width always.
- **Badges.** radius-sm chip, 11px, triplet colors, optional leading dot —
  the dakit `.dk-badge` recipe. A badge appears only when it changes what the
  operator does next; no pill on every row by default.
- **Containers.** One bordered container with divided rows; header/footer
  bands sit on `--dk-bg-muted`. Row-as-its-own-card is banned. Lists convert
  to labelled, edge-to-edge rows on phones.
- **Summary strips.** Counts render as one compact segmented strip (divided
  cells: dot + label + mono value), never as a row of equal stat boxes.
- **Forms.** Labels above controls without trailing colons; helper text
  directly below its control; stacked fields, 2–3 columns only for short
  comparable fields; one footer action row above a top border — primary
  first, then Cancel (`.dk-form-actions`).
- **Disclosures.** `<details>` rows are quiet rows inside their container:
  chevron icon + label, hover background, no per-item border box stack.
- **Sidebar.** 268px (`--dk-size-sidebar`), muted surface, right border;
  icon-plus-text nav rows (20px, stroke 1.8, 24-grid paths), radius-md rows,
  selection = accent-soft fill + accent text only (no stripes/rails);
  uppercase 12px group labels.
- **Icons.** One set, family geometry: inline SVG, 24×24 viewBox paths at
  20px, `stroke="currentColor"`, stroke-width 1.8, round caps/joins, no
  fills. The shared shapes live in `templates/mailing/operator/_icons.html`;
  no emoji, no second set, no icon font.

## Banned list (must never reappear)

1. Pill badges (`border-radius: 999px`) and pale chips on every row.
2. A filled-red destructive button inside table/list rows.
3. Bare `<button>` elements rendering as primary actions; more than one
   primary per view.
4. Equal-width stat-box grids (3-up "Quick Links", 4-up counts, 6-up metrics).
5. Disclosure blocks as consecutive bordered boxes.
6. Colon-suffixed form labels ("Name:").
7. App-local hex values, radii, shadows, or control sizes outside `--dk-*`.
8. Duplicated status badges saying the same thing twice in one header.
9. Mono type for sentence-length prose.
10. An explainer sentence stacked under every heading — one muted line,
    only when it changes how the operator uses the page.
