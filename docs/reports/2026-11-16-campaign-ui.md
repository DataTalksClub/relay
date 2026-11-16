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
