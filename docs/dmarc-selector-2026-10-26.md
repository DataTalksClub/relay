# Unknown AISHL DKIM selector: investigation and monthly review

Checked 2026-10-26 against the existing Relay deployment and retained Google
aggregate report `5681541082531485984` (report 303, record 924, attachment 303).
Sender ownership remains unverified. Intake `fb2f5fa6-8c98-4853-80c3-bdd4e86466f1`
must stay open pending the team's answer.

One-line team question: Does anyone on the team or any contractor use DKIM
selector `sales-a_ikebe`, an `ares.eonet.ne.jp` mailbox, or a personal forwarding
setup that sends as `aishippinglabs.com`?

## What the evidence establishes

On 2026-08-01, Google reported one message with header domain
`aishippinglabs.com` from `210.130.137.11`. Current PTR is
`hsmtpd-dfwd-c1101.xspmail.jp`. DKIM for `aishippinglabs.com` with selector
`sales-a_ikebe` failed. SPF passed for `ares.eonet.ne.jp` but failed DMARC
alignment. Both aligned checks failed.

The original XML reports disposition `none`, reason `forwarded`, comment
`looks forwarded, not quarantined for DMARC`. This contradicts the earlier
investigation's claim of "failed closed / no impact". No DMARC quarantine was
reported. The aggregate cannot establish inbox placement, recipient identity,
or sender authorization. Treat it as an unverified forwarding-associated
signature; do not classify it as confirmed authorized or unauthorized.

On 2026-10-26, both TXT and CNAME queries for
`sales-a_ikebe._domainkey.aishippinglabs.com` returned NXDOMAIN. The domain's
DMARC record remains `p=quarantine`. Current DNS does not prove historical
record state. No public key is available or authenticated for this selector.
Do not publish a key or change sending infrastructure before the owner and
legitimate signing domain are established.

The retained report is an aggregate about the original message, not the
original message itself. Its archive and raw download contain the reporting
email and aggregate attachment; they cannot supply the original sender's
headers or prove recipient inbox placement.

## Monthly five-minute checklist

Cadence: first day of each month, review the previous month. Next review:
2026-11-01. The recurrence is automated: a read-only check script
(`scripts/dmarc_monthly_check.sh`) runs from the operator crontab on the
first of each month at 09:00 Europe/Berlin, writes a dated report to
`~/tmp/agent-reports/dmarc-monthly/` and a one-line summary to
`~/.local/state/dmarc-monthly-check.log`, and flags any selector other than
the known SES one seen in the last 30 days. It performs no writes, blocks,
deletions, or notifications; a flagged report still needs a human to
identify the owner before any DNS or policy change. The human checklist
below stays the procedure for acting on a flagged report.

1. Open `/dmarc/summary/?domain=aishippinglabs.com&days=30` while signed in.
   Record report count, message count, DMARC pass rate, and latest report date.
   If reports are stale or absent, investigate ingestion before drawing a
   conclusion about sender activity. Use 90 days when necessary to cover a
   longer calendar month.
2. Open the report list and relevant report detail pages. The summary does
   not list selectors, so inspect DKIM domain/selector/result in detail for
   failures and unfamiliar sources. Compare against known SES selectors from
   the previous review and configured DNS. Missing selectors in reports are
   not automatically unknown signers.
3. Track `sales-a_ikebe` recurrence, source IP, envelope domain, alignment,
   disposition, and any forwarding override in the original aggregate XML.
   The parsed detail omits override reasons; download the attachment when
   needed. Do not publish raw mail bodies or credentials.
4. Record new unknown selectors and ask the owner before any alignment change.
   A familiar forwarding IP or SPF pass for another domain does not establish
   authorization. No blocks, deletion, or policy tightening follows from this
   checklist alone.

First check, 2026-10-26 02:59 UTC: deployed summary rendered HTTP 200 against the
live database using a read-only transaction and an unsaved staff user (no
persisted login). Last 30 days: 58 reports, 823 messages, 100.0% DMARC pass,
zero DKIM-fail rows, three SPF-fail rows, zero failing source IPs. Reports span
2026-04-23 through 2026-10-24. Historical selector `sales-a_ikebe` occurs once;
no recurrence appears in the last 30 days. Known aligned SES selector:
`zma53mzn7ar2w67s64cmer7dfujap6kw`. Synthetic proof reports are included in
these dashboard totals; do not treat them as exclusively real traffic.

Re-check, 2026-10-30 08:49 UTC (also the first automated run of
`scripts/dmarc_monthly_check.sh`, report
`~/tmp/agent-reports/dmarc-monthly/2026-10-30.md`): selector DNS still
NXDOMAIN for TXT and CNAME; `_dmarc` still `p=quarantine`; PTR for
210.130.137.11 still `hsmtpd-dfwd-c1101.xspmail.jp`; public summary route
still login-protected (HTTP 302). Deployed summary rendered HTTP 200
read-only against the live production database: last 30 days 53 reports,
737 messages, 100.0% DMARC pass; selector census all-time
`sales-a_ikebe` 1 message, SES selector 36,266, no-DKIM 11; last 30 days
only the SES selector plus one no-DKIM record — zero unknown selectors,
zero `sales-a_ikebe` recurrence; ingestion current (newest report
2026-10-28).

## Remaining acceptance work

- Obtain the team's answer to the question above. If legitimate, establish the
  signing domain and key with its owner and choose verified alignment or SES
  migration. No DNS change is justified yet.
- If unauthorized, record that human confirmation. The existing aggregate
  still cannot prove "no inbox impact"; recipient evidence would be needed.

The monthly recurrence is set up (crontab entry of 2026-10-30 running
`scripts/dmarc_monthly_check.sh` on the 1st at 09:00 Europe/Berlin; installed
copy `/home/alexey/tmp/bin/dmarc-monthly-check.sh` must match the committed
file byte for byte). The first automated run on 2026-10-30 is recorded above;
the first scheduled run fires 2026-11-01. A human calendar reminder was not
created (no access to Alexey's calendar); the cron job and its report/log
files are the standing reminder.

No application code, runtime configuration, AWS access, DNS changes, blocking,
or deletion was needed for this investigation. The accompanying private
operator report records the exact live commands and sanitized output.
