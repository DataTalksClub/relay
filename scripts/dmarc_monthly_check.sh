#!/usr/bin/env bash
# Monthly DMARC selector review for aishippinglabs.com (docs/dmarc-selector-2026-10-26.md).
#
# Strictly read-only: discovers the current relay production web container on
# contabo-b by its Coolify labels, runs a READ ONLY database transaction plus
# unsaved in-memory staff user to render the deployed /dmarc/ summary views,
# and writes a dated report file. No database writes, no DNS or container
# changes, no notifications to people.
#
# Scheduled monthly (1st 09:00 Europe/Berlin) via the operator crontab; the
# installed copy lives at /home/alexey/tmp/bin/dmarc-monthly-check.sh and must
# match this file byte for byte.
# Failure check for humans: grep 'status=FAIL' /home/alexey/.local/state/dmarc-monthly-check.log
# Exit codes: 0 ok; 1 findings (unknown selector / stale ingestion / bad summary);
#             2 infrastructure failure (ssh, container, lock, or report write).

set -uo pipefail

DOMAIN="aishippinglabs.com"
SSH_TARGET="${DMARC_SSH_TARGET:-contabo-b}"
COOLIFY_RESOURCE="${DMARC_COOLIFY_RESOURCE:-relay-staging}"
KNOWN_SELECTORS="${DMARC_KNOWN_SELECTORS:-zma53mzn7ar2w67s64cmer7dfujap6kw}"
REPORT_DIR="${DMARC_REPORT_DIR:-/home/alexey/tmp/agent-reports/dmarc-monthly}"
LOG_FILE="${DMARC_LOG_FILE:-/home/alexey/.local/state/dmarc-monthly-check.log}"
LOCK_FILE="${DMARC_LOCK_FILE:-/home/alexey/.local/state/dmarc-monthly-check.lock}"
SSH_DEADLINE="${DMARC_SSH_DEADLINE:-240}"

mkdir -p "$REPORT_DIR" "$(dirname "$LOG_FILE")" "$(dirname "$LOCK_FILE")" || exit 2
exec 9>"$LOCK_FILE" || exit 2
flock -n 9 || { echo "$(date -Is) status=FAIL reason=lock-held report=none" >> "$LOG_FILE"; exit 2; }

fail() { echo "$(date -Is) status=FAIL reason=$1 report=${2:-none}" >> "$LOG_FILE"; exit 2; }

TODAY="$(date +%F)"
OUT="$REPORT_DIR/$TODAY.md"
TMP="$(mktemp "$REPORT_DIR/.check-$TODAY-XXXXXX.md")" || fail mktemp

ssh_opts=(-o BatchMode=yes -o ConnectTimeout=15)

CONTAINER="$(timeout "$SSH_DEADLINE" ssh "${ssh_opts[@]}" "$SSH_TARGET" \
  "sudo -n docker ps --filter label=coolify.resourceName=$COOLIFY_RESOURCE --filter label=coolify.serviceName=web --format '{{.Names}}' | head -n1")" \
  || CONTAINER=""
if [ -z "$CONTAINER" ]; then
  rm -f "$TMP"
  fail "no-web-container-$COOLIFY_RESOURCE"
fi

CHECK_RC=0
CHECK="$(timeout "$SSH_DEADLINE" ssh "${ssh_opts[@]}" "$SSH_TARGET" \
  "sudo -n docker exec -i -e DMARC_KNOWN_SELECTORS='$KNOWN_SELECTORS' -e DMARC_CHECK_DOMAIN='$DOMAIN' $CONTAINER python manage.py shell" <<'PY'
import json, os
from collections import Counter
from datetime import timedelta
from django.utils import timezone
from django.db import connection, transaction
from django.test import RequestFactory
from django.contrib.auth import get_user_model
from mailing.models import DmarcRecord, DmarcReport
from mailing.views import dmarc_summary
from unittest.mock import patch

DOMAIN = os.environ.get("DMARC_CHECK_DOMAIN", "aishippinglabs.com")
KNOWN = {s for s in os.environ.get("DMARC_KNOWN_SELECTORS", "").split(",") if s}

with transaction.atomic():
    with connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
    print("CHECKED_AT", timezone.now().isoformat())
    qs = DmarcRecord.objects.select_related("report", "report__attachment__archive__domain").filter(
        report__attachment__archive__domain__name=DOMAIN)
    selectors = Counter()
    recent = Counter()
    unknown_records = []
    recurrence = []
    since = timezone.now() - timedelta(days=30)
    for row in qs.iterator():
        for sig in row.auth_results.get("dkim", []):
            if sig.get("domain") != DOMAIN:
                continue
            selector = sig.get("selector", "")
            selectors[selector] += row.count
            in_window = row.report.date_end >= since
            if in_window:
                recent[selector] += row.count
            if in_window and selector and selector not in KNOWN:
                unknown_records.append({"selector": selector, "record_id": row.pk,
                                        "report_pk": row.report_id, "reporter": row.report.reporter_org,
                                        "date_end": row.report.date_end.isoformat(),
                                        "source_ip": row.source_ip, "count": row.count,
                                        "disposition": row.disposition, "dkim": row.dkim, "spf": row.spf})
            if selector == "sales-a_ikebe":
                recurrence.append({"record_id": row.pk, "report_pk": row.report_id,
                                   "date_end": row.report.date_end.isoformat(), "count": row.count})
    print("RECENT_SELECTORS", json.dumps(dict(recent), sort_keys=True))
    print("ALL_SELECTORS", json.dumps(dict(selectors), sort_keys=True))
    print("UNKNOWN_SELECTORS", json.dumps(unknown_records, sort_keys=True))
    print("SALES_A_IKEBE", json.dumps(recurrence, sort_keys=True))
    reports = DmarcReport.objects.filter(attachment__archive__domain__name=DOMAIN)
    newest = reports.order_by("-date_end").values_list("date_end", flat=True).first()
    print("COVERAGE", json.dumps({"reports": reports.count(), "newest": str(newest)}))
    stale = newest is None or newest < timezone.now() - timedelta(days=8)
    print("INGESTION", "stale" if stale else "current")
    rf = RequestFactory()
    user = get_user_model()(is_staff=True, is_active=True, is_superuser=True)
    request = rf.get("/dmarc/summary/", {"domain": DOMAIN, "days": "30"})
    request.user = user
    request.session = {}
    captured = {}

    def render(request, template, context):
        captured.update({"reports": context["report_count"], "by_domain": context["by_domain"],
                         "failing_ips": context["failing_ips"]})
        from django.shortcuts import render as real_render
        return real_render(request, template, context)

    with patch("mailing.views.render", render):
        response = dmarc_summary(request)
    print("SUMMARY", json.dumps({"path": request.get_full_path(), "status": response.status_code, **captured},
                                sort_keys=True))
PY
)" || CHECK_RC=$?
if [ "$CHECK_RC" -ne 0 ]; then
  CHECK="$CHECK
SHELL_EXIT_$CHECK_RC"
fi

UNKNOWN_COUNT="$(printf '%s\n' "$CHECK" | sed -n 's/^UNKNOWN_SELECTORS //p' | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo "unparsed")"
INGESTION="$(printf '%s\n' "$CHECK" | sed -n 's/^INGESTION //p')"
SUMMARY_STATUS="$(printf '%s\n' "$CHECK" | sed -n 's/^SUMMARY //p' | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' 2>/dev/null || echo "unparsed")"

STATUS=OK
EXIT_CODE=0
REASON=""
if [ "$CHECK_RC" -ne 0 ] || [ "$UNKNOWN_COUNT" = "unparsed" ] || [ -z "$INGESTION" ] || [ "$SUMMARY_STATUS" = "unparsed" ]; then
  STATUS=FAIL
  EXIT_CODE=2
  REASON="remote-rc-$CHECK_RC-parse-unknown=$UNKNOWN_COUNT-ingestion=${INGESTION:-missing}-summary=$SUMMARY_STATUS"
elif [ "$UNKNOWN_COUNT" != "0" ] || [ "$INGESTION" != "current" ] || [ "$SUMMARY_STATUS" != "200" ]; then
  STATUS=FAIL
  EXIT_CODE=1
  REASON="findings-unknown=$UNKNOWN_COUNT-ingestion=$INGESTION-summary=$SUMMARY_STATUS"
fi

(
  set -e
  echo "# DMARC monthly selector check — $TODAY"
  echo
  echo "- Domain: \`$DOMAIN\`"
  echo "- Container: \`$CONTAINER\` on \`$SSH_TARGET\` (Coolify resource \`$COOLIFY_RESOURCE\`)"
  echo "- Known selectors: \`$KNOWN_SELECTORS\`"
  echo "- Unknown selectors in last 30 days: $UNKNOWN_COUNT"
  echo "- Ingestion: ${INGESTION:-unknown}"
  echo "- Deployed summary HTTP status: $SUMMARY_STATUS"
  echo "- Remote exit code: $CHECK_RC"
  echo "- Status: $STATUS"
  echo
  if [ "$STATUS" != "OK" ]; then
    echo "**ATTENTION: this run needs a human — see docs/dmarc-selector-2026-10-26.md. Unknown selector(s) mean an unidentified sender (identify the owner before any DNS or policy change); stale ingestion, a bad summary, or a remote failure means the check itself is not trustworthy.**"
    echo
  fi
  echo '```'
  printf '%s\n' "$CHECK"
  echo '```'
) > "$TMP"
WRITE_RC=$?
if [ "$WRITE_RC" -ne 0 ]; then
  rm -f "$TMP"
  fail report-write
fi
mv "$TMP" "$OUT" || fail report-rename "$OUT"

LOG_LINE="$(date -Is) status=$STATUS"
[ -z "$REASON" ] || LOG_LINE="$LOG_LINE reason=$REASON"
LOG_LINE="$LOG_LINE remote_rc=$CHECK_RC unknown=$UNKNOWN_COUNT ingestion=${INGESTION:-unknown} summary=$SUMMARY_STATUS container=$CONTAINER report=$OUT"
echo "$LOG_LINE" >> "$LOG_FILE" || exit 2
echo "Report written: $OUT (status=$STATUS)"
printf '%s\n' "$CHECK"
exit "$EXIT_CODE"
