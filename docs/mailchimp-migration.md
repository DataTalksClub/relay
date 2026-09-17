# Mailchimp Migration

Date: 2026-08-09

Status: planned. Relay currently has only a sandbox deployment. The production
sending path described here does not exist yet.

## Goal

Migrate the established DataTalksClub newsletter from Mailchimp to Amazon SES
through Relay without sending duplicate newsletters, losing suppression state,
or moving the entire audience to a new sending path at once.

Current planning assumptions:

- approximately 130,000 newsletter subscribers;
- one regular newsletter on Mondays at 10:00 in `Europe/Berlin`;
- approximately 5-7 sends per month;
- Mailchimp and Relay operate in parallel during the migration;
- each eligible address is routed through exactly one provider for a campaign.

Use the IANA timezone `Europe/Berlin`, not a fixed CET offset, so the schedule
continues to follow daylight-saving changes.

## Related AWS Support Request

The SES quota-increase request, AWS Support case IDs, request IDs, and the full
operational description sent to AWS are recorded in the infrastructure
repository:

- [`DataTalksClub/aws-infra/docs/aws-support/2026-08-09-ses-newsletter-quota-increase.md`](https://github.com/DataTalksClub/aws-infra/blob/main/docs/aws-support/2026-08-09-ses-newsletter-quota-increase.md)
- daily sending quota case: `178628060500737`;
- sending-rate case: `178628058900407`;
- requested quotas in the main account, `eu-west-1`: 200,000 recipients per
  rolling 24 hours and 200 recipients per second.

The support record is the source of truth for AWS's response and the final
approved limits. Relay must read and enforce the live approved rate rather than
assuming the requested values were granted.

## Migration Readiness

Do not begin the production audience split until all of these are complete:

1. Deploy a production Relay sending path in AWS account `387546586013`, region
   `eu-west-1`. The current Relay sandbox sends through the sandbox account's
   `dtcdev.click` identity in `us-east-1`; it cannot consume the quota requested
   for the main account.
2. Send from an authenticated DataTalksClub newsletter identity with aligned
   DKIM, SPF, DMARC, and custom MAIL FROM configuration.
3. Route SES configuration-set delivery, bounce, complaint, reject, and
   rendering-failure events into Relay's durable ingress path.
4. Verify that permanent bounces and complaints suppress later sends and that
   soft bounces do not become permanent suppressions.
5. Add RFC 8058 one-click unsubscribe headers to Relay campaign messages, in
   addition to the existing visible HTML and text preference link.
6. Verify production queue throttling, retries, idempotency, dead-letter
   handling, monitoring, and operator alerts.
7. Add or verify an operator-safe way to create the deterministic migration
   cohort and to export recipients that were provably never accepted by SES.

## One Routing Tag

Use one cumulative Mailchimp tag as the provider-routing boundary:

```text
delivery-relay
```

For every dual-provider campaign:

```text
Relay campaign:
  include tag = delivery-relay

Mailchimp campaign:
  send to = subscribed audience
  do not send to = delivery-relay
```

The tag is cumulative. Once an address is assigned to Relay, do not remove the
tag during the migration. This rule gives the systems complementary datasets:

```text
Relay recipients intersect Mailchimp recipients = empty set

Relay recipients union Mailchimp recipients = eligible newsletter audience
```

Do not create two independent random samples before each campaign. Re-sampling
would move contacts between providers and could cause duplicates or gaps.

## Deterministic Random Cohorts

Start from contacts that are currently eligible for the newsletter. Exclude
unsubscribed, cleaned, invalid, hard-bounced, complained, and otherwise
suppressed contacts before assigning a cohort.

Normalize each eligible address and assign a stable bucket:

```python
from hashlib import sha256


def migration_bucket(email):
    normalized = email.strip().casefold()
    digest = sha256(normalized.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100
```

The active Relay cohort is cumulative:

```text
5%:   bucket < 5
10%:  bucket < 10
25%:  bucket < 25
50%:  bucket < 50
75%:  bucket < 75
100%: bucket < 100
```

Hash buckets produce an approximately even and reproducible random split. They
also give each cohort a representative mix of mailbox providers without
storing raw addresses in a cohort manifest. Record the actual eligible count
at each stage; percentages are based on current eligible subscribers, not the
historic headline audience size.

With exactly 130,000 eligible subscribers, the expected progression is:

| Campaign | Relay total | Newly moved to Relay | Mailchimp remainder |
|---|---:|---:|---:|
| 1 | about 6,500 (5%) | about 6,500 | about 123,500 (95%) |
| 2 | about 13,000 (10%) | about 6,500 | about 117,000 (90%) |
| 3 | about 32,500 (25%) | about 19,500 | about 97,500 (75%) |
| 4 | about 65,000 (50%) | about 32,500 | about 65,000 (50%) |
| 5 | about 97,500 (75%) | about 32,500 | about 32,500 (25%) |
| 6 | about 130,000 (100%) | about 32,500 | 0 |

Advance only after reviewing the previous Relay campaign over the following
week. A same-day lack of errors is not sufficient evidence for the next stage.

## Preparing Each Split

Mailchimp remains the temporary routing source of truth while both providers
are active.

1. Export the complete Mailchimp audience, including subscribed,
   unsubscribed, and cleaned contacts.
2. Calculate the current deterministic cohort from eligible subscribed
   contacts.
3. Add `delivery-relay` in Mailchimp to contacts whose bucket is below the
   current threshold. Never remove it from an earlier cohort.
4. Export the complete Mailchimp audience again after the tag update. A full
   export is required so Relay receives current subscribed, unsubscribed,
   cleaned, and tag state together.
5. Transfer the export securely to Relay. Do not commit the export or raw email
   manifests to Git.
6. Dry-run the Relay import:

   ```bash
   uv run python manage.py import_mailchimp_zip \
     --zip /secure/path/mailchimp-export.zip \
     --organization datatalksclub \
     --audience dtc-newsletter \
     --client dtc-newsletter \
     --dry-run \
     --report /tmp/mailchimp-import-dry-run.json
   ```

7. Review invalid rows and subscribed, unsubscribed, cleaned, tag, and
   subscription counts. Then perform the import:

   ```bash
   uv run python manage.py import_mailchimp_zip \
     --zip /secure/path/mailchimp-export.zip \
     --organization datatalksclub \
     --audience dtc-newsletter \
     --client dtc-newsletter \
     --report /tmp/mailchimp-import.json
   ```

8. Create matching Relay and Mailchimp campaign drafts with the same subject,
   sender identity, and provider-neutral content. Each provider owns its own
   unsubscribe implementation; do not paste Mailchimp unsubscribe merge tags
   into Relay content.
9. In Relay, select `delivery-relay` as the only migration include tag. Review
   Queue Preview, its queueable count, and every suppression/skip-reason count.
10. In Mailchimp, select the subscribed audience and set `delivery-relay` in
    **Do not send to**. Review the final recipient count.

## Pre-send Partition Check

Record these values for every campaign:

```text
N = eligible audience at the split snapshot
R = Relay queueable recipients
M = Mailchimp final recipients
S = contacts excluded by current suppression state
```

The expected count invariant is:

```text
R + M + S = N
```

Counts are a minimum check. Before the first production send, compare hashed
normalized-email manifests from the final Relay and Mailchimp recipient sets:

```text
intersection(relay_hashes, mailchimp_hashes) = empty set
```

Do not send if the intersection is non-empty or if the unexplained difference
between the union and the eligible audience is non-zero.

## Monday Send Procedure

Both complementary campaigns can start at the normal Monday 10:00
`Europe/Berlin` time:

1. Queue the Relay campaign with `delivery-relay` included.
2. Send the Mailchimp campaign with `delivery-relay` excluded.
3. Monitor Relay campaign progress, SES accepts, delivery events, failures,
   hard bounces, complaints, unsubscribes, queue age, and dead-letter queues.
4. Verify that the Mailchimp and Relay final recipient counts still match the
   recorded partition.
5. Evaluate Relay results over the full week before increasing the cohort for
   the next campaign.

Recommended promotion gates:

- SES account and configuration-set reputation remain healthy;
- hard-bounce rate remains below 2%;
- complaint rate remains comfortably below 0.1%, with a target below 0.05%;
- no unexplained rejects, throttling, stalled queue, or dead-letter backlog;
- unsubscribe and provider-event suppressions work end to end;
- test and real delivery are acceptable across major mailbox providers.

If a gate fails, keep the same cohort percentage or reduce sending while the
cause is investigated. Do not expand the tag merely because SES accepted every
API request.

## Mailchimp Fallback

Mailchimp sends the non-Relay portion regardless of Relay's health. A fallback
campaign is only for Relay-cohort recipients that were provably never accepted
by SES.

Use the SES message ID as the acceptance boundary:

| Relay result | Mailchimp fallback |
|---|---|
| Has an SES message ID or status `sent` | No |
| Delivered | No |
| Pending or retrying | No; wait or cancel retries first |
| Hard bounced | Never |
| Complained, unsubscribed, or suppressed | Never |
| Permanently failed with no SES message ID | Eligible after a final suppression check |
| Relay failed before sending any message | Eligible after the Relay campaign is cancelled and drained |

Never resend an address merely because a delivery event has not arrived. SES
may still deliver an accepted message, which would make a Mailchimp fallback a
duplicate.

For a partial failure:

1. cancel or pause the Relay campaign;
2. wait until queued work and retries have stopped;
3. export only contacts with no SES message ID that remain eligible and are not
   suppressed;
4. use that exact list for a separate Mailchimp fallback campaign;
5. record the fallback campaign and its recipient manifest in the migration
   audit.

## Final Cutover

At 100%, all currently eligible subscribers carry `delivery-relay`, and the
regular Mailchimp campaign has zero recipients after applying the exclusion.

Before retiring Mailchimp:

1. import one final complete Mailchimp export;
2. reconcile subscribed, unsubscribed, and cleaned totals;
3. verify all Relay suppressions and event consumers;
4. make Relay the source of truth for newsletter consent and preferences;
5. update signup integrations so new contacts enter Relay directly;
6. retain the Mailchimp export and migration audit according to the applicable
   data-retention policy;
7. disable Mailchimp sending only after at least one successful 100% Relay
   campaign and an agreed observation period.

## External References

- [Mailchimp: Send to tags](https://mailchimp.com/help/send-tags/)
- [Mailchimp: View or export contacts](https://mailchimp.com/help/view-export-contacts/)
- [Google: Email sender guidelines](https://support.google.com/mail/answer/81126)
- [AWS: Managing SES sending limits](https://docs.aws.amazon.com/ses/latest/dg/manage-sending-quotas.html)
- [AWS: Monitoring SES sender reputation](https://docs.aws.amazon.com/ses/latest/dg/monitor-sender-reputation.html)
