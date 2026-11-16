# Campaign detail UI — intake 643cf759

Scope: campaign detail presentation and read-only metrics. No sending code changes.
Campaign 255 / weekly-303 must never receive campaign actions from this session.
Production promotion is authorized by the intake but strictly gated on authenticated
GET /api/campaigns/weekly-303?client=dtc-newsletter&audience=dtc returning status sent.

Plan: Delivery first (sent / pending / total, progress, recent send rate), Engagement
next with bars; keep actions, preview, recipients, links and diagnostics accessible
in secondary disclosures. Preserve draft review and confirmation workflow.

Before/after desktop and mobile screenshots: ~/tmp/agent-reports/2026-11-16-relay-campaign-ui/.
Validation and release evidence will be appended as work completes.

Implementation: Delivery is the first content block, with accepted send count from
recipient sent_at evidence (including subsequent unsubscribes), pending, total,
and an accessible progress element. Recent rate counts this campaign's sent_at
in the last 60 seconds; inactive campaigns report zero and do not query the window.
Engagement immediately follows with three accessible bars. Detailed stats, timing,
message/test preview, controls, recipients and history remain in disclosures.
Recipient filters/search/page URLs automatically open their disclosure; link stats
load on expanding their section. Draft/scheduled confirmation paths remain intact.

Validation: existing campaign/operator/link/workflow suites 141 passed; seven
focused tests additionally cover scoped recent rate, stale/future send timestamps,
paused/sent/cancelled campaigns, empty/snapshotting campaigns, evidence-based sent
counts, section order and filter disclosures. Ruff and diff whitespace checks pass.

Screenshots are under /home/alexey/tmp/agent-reports/2026-11-16-relay-campaign-ui/.
`before/after-*.png` use an isolated local fixture; `before-live-*.png` render the
existing live campaign using a read-only PostgreSQL transaction; `proposed-live-*`
render the proposed template against live data in memory (no deployed file edits).
Browser checks confirm 1440px desktop / 390px mobile without horizontal overflow.
