# Relay business metrics (collect only)

The collector lives in `coolify-observability/biz-metrics/`, alongside its
PostgreSQL fixture tests. Relay supplies the source tables; collection uses
the existing shared runner and requires no Relay migration or app endpoint.

Daily aggregates are stored in `biz_metrics.metric_daily` on pg-shared with
`project='relay'`. The same values are imported into VictoriaMetrics with a
`biz_` prefix and timestamps at the start of their UTC day.

| Metric | Dimensions | Source and day rule |
|---|---|---|
| `relay_tx_messages` | client, status, template_key | `transactional_messages.created_at`, grouped by current status |
| `relay_campaign_recipients` | client, status | `campaign_recipients` joined through `campaigns`; sent by `sent_at`, other non-pending statuses by `updated_at` |
| `relay_bounce_rate` | client, stream | bounce events divided by sent events in `email_events.created_at` on the same day |
| `relay_complaint_rate` | client, stream | complaint events divided by sent events in `email_events.created_at` on the same day |

`client` is `clients.slug`. Stream is `tx` when `transactional_message_id` is
set, otherwise `campaign` when `campaign_id` is set. Events with neither
reference cannot be assigned a catalog stream and are excluded. Rates count
event rows, not distinct messages. A day without sent events has an undefined
rate and produces no stored row or VM sample. A stream with sends and no
bounces/complaints produces a zero rate. A campaign day without recipients
also produces no row; missing data must not be presented as a measured rate.
While the `campaign_recipients` table has no rows at all,
`relay_campaign_recipients` is **expected-empty**: it yields no `metric_daily`
rows and no `biz_relay_campaign_recipients` VM series, and this state is the
accepted baseline (developer decision, 2026-10-26). The SQL is a plain
aggregate over the source, so the series reappears automatically on the next
collection that covers a UTC day with qualifying recipients — non-pending
rows counted by the day rules above — with no collector change needed.

Labels contain only the dimensions above. Email addresses, user IDs, contact
IDs, IPs and message/campaign identifiers are never labels. There are no alert
rules or threshold checks in this collection job.

## Backfill and verification

On contabo-b, `/data/biz-metrics/run.sh` loads the node's private configuration
and resolves the current database container addresses. Credentials stay on
that node. The nightly job recomputes the last seven completed UTC days;
the first backfill must explicitly start at the earliest relevant source day.
The end date is exclusive:

```bash
sudo /data/biz-metrics/run.sh --since YYYY-MM-DD --until YYYY-MM-DD \
  --metrics relay_tx_messages relay_campaign_recipients \
  relay_bounce_rate relay_complaint_rate
```

The runner replaces each metric's daily slice before upserting, so a changed
message status removes its previous dimension. Repeating the same interval
should preserve row keys and values. Compare the entire requested interval
with source SQL, including days that return no rows.

For an independent historic-day check in an authorized SQL console/admin
connection to the Relay database, count by every catalog dimension:

```sql
SELECT c.slug AS client, tm.status, tm.template_key, count(*) AS value
FROM transactional_messages tm
JOIN clients c ON c.id = tm.client_id
WHERE tm.created_at >= TIMESTAMPTZ '2026-10-20 00:00:00+00'
  AND tm.created_at < TIMESTAMPTZ '2026-10-21 00:00:00+00'
GROUP BY c.slug, tm.status, tm.template_key
ORDER BY 1, 2, 3;
```

Compare with a separate connection to the aggregate database:

```sql
SELECT dims->>'client' AS client, dims->>'status' AS status,
       dims->>'template_key' AS template_key, value
FROM metric_daily
WHERE day = DATE '2026-10-20'
  AND project = 'relay' AND metric = 'relay_tx_messages'
ORDER BY 1, 2, 3;
```

In VMUI, select yesterday's **UTC** range and query each `biz_relay_*` name.
For an instant query, use yesterday at 00:01 UTC because imported samples
have midnight timestamps. Check the source's campaign counts and scoped sent
events when a series is absent. Do not create sample campaign recipients or
send events to satisfy a visibility check. Record missing observations as an
acceptance blocker if all four series are required, except
`biz_relay_campaign_recipients` while `campaign_recipients` is empty: that
absence is the expected-empty baseline above, not a blocker.

The shared suite is `biz-metrics/tests/` in coolify-observability. Set
`BIZMETRICS_TEST_DSN` to an isolated PostgreSQL test database to exercise the
real SQL, UTC bounds, stream assignment, zero-send behavior and repeated
backfills after status changes. Never point fixture tests at an app database.
