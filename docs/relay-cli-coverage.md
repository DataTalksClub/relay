# Relay web and CLI coverage inventory

This inventory compares operator actions exposed by the Relay UI with their
API and CLI paths. It describes the repository state while the CLI work is in
progress; “partial” means the CLI does not yet cover every UI action.

| Area | Web actions | API coverage | CLI coverage |
|---|---|---|---|
| Campaigns | List, create/edit drafts, preview, test send, schedule/queue, cancel, pause/resume/stop, inspect recipients, retry/resolve recipients, statistics | Client campaign API supports get/upsert, preview, test send, queue, cancel/pause/resume/stop, recipient listing/retry and unknown-recipient reconciliation. Admin API adds list/detail/overview, draft writes, queue and recipient assume-sent. | Partial: `operator campaigns` covers list/show/create/edit/stats/queue, preview/test-send, cancel/pause/resume/stop, recipients, recipient-retry, assume-sent and unknown-recipient reconciliation. Schedule inspection lives under `operator schedules list/show`. |
| Templates | Browse catalog, inspect template/version history, preview and test send; edit through Django admin | Admin API lists/details templates and now accepts create/update/delete for regular email templates. Client transactional-template API supports list/get/upsert, publish, versions, preview and test send. | Partial: `operator templates` covers catalog/detail, regular email-template create/edit/delete, and transactional template upsert/publish/versions/preview/test-send. Template version history is available for transactional templates. Regular email-template revision history stays UI-only because the admin API exposes no revisions endpoint; covering it needs new server-side API work. |
| Contacts and lists | Search and inspect contacts/history, update state/subscriptions/tags; create/edit audiences and tags | Contact export/import/status/erase/preferences/tag/history and state endpoints exist. Admin API exposes contact/audience/tag records, overview and writes for audiences/tags; client recipient-list APIs support members and imports. | Partial: `operator contacts` covers list/detail/overview, state, subscriptions, tags and contact API actions. `operator lists` covers audiences/tags plus audience summaries through `operator lists overview`, recipient-list membership/reconcile/import and transactional list send. Contact CSV import/export is covered by `operator contacts csv-export` and `csv-import`. Public subscribe/unsubscribe stays a public web workflow; it is not an operator action, so the CLI does not wrap it. |
| Newsletters | Create/edit issues and content blocks, reorder/delete blocks, preview and test send | Newsletter issue/block/type/preview/test-send API endpoints exist. | Partial: `operator newsletters` supports issue and block CRUD, preview, block types and test-send. Newsletter UI send history is not directly available. |
| Sends and activity | Transactional queue, message details/retry, campaign results and email activity | Transactional send/status/template APIs and admin message retry/read endpoints exist. | Covered: `send`, `status`, `operator messages list/show/retry/bulk-retry`, email activity through `operator events list` and `operator messages list`, and campaign result workflows through campaign overview/stats/recipients/assume-sent. |
| Statistics | Dashboard, audience summaries, campaign rates and recipient progress | Admin dashboard and contact/audience/campaign overview endpoints exist. | Covered: `operator stats list` (dashboard), `operator stats workers` (service health) and campaign/contact/audience overview commands. |
| Complaints | Complaint dashboard and contact complaint state | Contact search supports `suppression=complained`; the complaints dashboard is a separate web view. | Partial: `operator complaints list/show/stats` covers contacts with complaint state. The complaints dashboard summary and alert actions stay web-only: the dashboard is a server-rendered view without a JSON endpoint, so there is no API for the CLI to call. |
| Archive export | Export Relay data for transfer | Admin `GET /api/admin/transfer/export` returns a manifest or paginated resource export. | Covered at the API level by `operator archive export` with `--resource-name`, `--offset` and `--limit`. |
| Inbound, domains and senders | Inbound routing and messages; configure sending domains and sender addresses | Existing admin/client inbound and sending-domain APIs; sender policy API. | Covered in part by `inbound` (domains, mailboxes, deliveries, DMARC archives and `dmarc-summary`), `domains` and `senders`. |
| Authentication | Browser sign-in for staff; approve/deny CLI device pairing | OIDC browser sign-in plus `/api/agent/device/{start,token,revoke}` and the staff-only `/device` approval page. Device codes expire after 10 minutes, approvals are rate-limited and audited, and the resulting admin API key is revocable. | `auth login/logout/status` uses `dtc_device_login.device_login` from DataTalksClub/auth and its shared code/URL/waiting UX. |

## Relevant route sources

- Web operator routes are registered in `mailing/urls.py` and
  `mailing/admin_urls.py`.
- Client API routes are documented in `docs/api.md` and generated from
  `mailing/urls.py`.
- Admin routes are protected by Relay admin API keys and listed by
  `GET /api/admin/`.
- CLI commands are implemented in `cli/datamailer_cli/cli.py`; the installed
  `relay` and legacy `datamailer` script names share the same entry point.

The CLI has partial coverage of the web UI. The remaining gaps are all
server-side: regular email-template revision history, newsletter issue send
history, and the complaints dashboard summary/alerts have no JSON API a CLI
could call; schedule state changes are web-admin actions; public
subscribe/unsubscribe is a public web workflow rather than an operator
action. Each needs new server-side API work before the CLI can cover it.
