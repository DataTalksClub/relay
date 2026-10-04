# Relay admin API

The management API lives under `/api/admin/`. It authenticates with dedicated
`relay_admin_...` Bearer keys. Client keys cannot authenticate here, and admin
keys cannot authenticate on the client API. Browser login cookies do not
authenticate the admin API.

Admin keys grant management access across organizations and clients. Each key
belongs to an active staff user; disabling that user or removing staff status
immediately disables their keys. Secrets are hashed in the database and displayed
only when created. Successful mutations record the staff user and admin key ID in
operator audits. List and detail responses omit credential hashes and secrets.

## Create the first key

Apply the migration and create a key for an existing staff account:

```bash
uv run python manage.py migrate
uv run python manage.py create_admin_api_key --user YOUR_USERNAME --name automation
```

The command prints the secret once. Store it securely. Staff can also create and
revoke keys at `/admin-api-keys/`, linked from the operator API docs page. The
management command can create a replacement if all existing keys are revoked.

```bash
export RELAY_ADMIN_API_KEY='<new-admin-key>'
export RELAY_URL='https://relay.dtcdev.click'
curl "$RELAY_URL/api/admin/" \
  -H "Authorization: Bearer $RELAY_ADMIN_API_KEY"
```

The authenticated index lists all routes. Paths below are relative to
`/api/admin/`, without trailing slashes except for the index itself. JSON writes
require `Content-Type: application/json`. Responses use `Cache-Control: no-store`.

## Management resources

| Resource | Operations | Write contract |
| --- | --- | --- |
| `organizations` | GET list, POST create; GET/PATCH `/{id}` | `name`, `slug` |
| `audiences` | GET list, POST create; GET/PATCH `/{id}` | `name`, `slug`, `organization_id` |
| `clients` | GET list, POST create; GET/PATCH `/{id}` | `name`, `slug`, `organization_id`, `is_active` |
| `clients/{id}/settings` | GET, PATCH | Operator client form fields, including sender configuration and write-only integration secrets |
| `api-keys` | GET list, POST create | `name`; new key belongs to the calling staff user |
| `api-keys/{id}/revoke` | POST | Revokes an admin key; repeat calls are harmless |
| `clients/{id}/api-keys` | GET list, POST create | `name`, optional `notes` |
| `clients/{id}/api-keys/{key_id}/revoke` | POST | Revokes that client's key |
| `tags` | GET list, POST create; GET/PATCH `/{id}` | `name`, `slug`, `audience_id` |
| `inbound-addresses` | GET list, POST create; GET/PATCH `/{id}` | `local_part`, `domain`, `note`, `is_active` |
| `blocked-senders` | GET list; GET/DELETE `/{id}` | DELETE unblocks the sender |

Create responses return 201. Key creation returns `api_key` once; subsequent
reads return only ID, name, safe prefix, creation/usage/revocation timestamps,
and the admin key's owner ID. PATCH preserves omitted settings. Organization
ownership cannot be changed on existing clients or audiences. Tag audience
ownership is also immutable.

Client settings use the same field names and validation as the operator form:
`organization`, `name`, `slug`, `default_sender_id`, `sender_emails`,
`cmp_webhook_url`, `cmp_webhook_token`, `mailchimp_api_key`, `mailchimp_list_id`,
`mailchimp_enabled`, `is_active`. Here `sender_emails` is newline-separated
`sender-id=Display Name <email@example.com>` text. An omitted secret is preserved;
an empty Mailchimp key preserves the current key, matching the UI. For structured
sender mappings use `clients/{id}/client/senders`, with the client API contract.

List responses contain `items`, `total`, `limit`, and `offset`. `limit` defaults to
100 and must be 1–200. Operator read collections accept the foreign-key filters
present on their projection (`client_id`, `audience_id`, `contact_id`,
`campaign_id`, `tag_id`, `transactional_message_id`). Contact search accepts the operator explorer filters, including `q`,
`subscription_status`, `verified`, suppression, validation, engagement and tag
filters. Inbound messages accept `state`, `q` and `address`. Scoped client operations retain their
existing pagination and filtering contracts.

## Import/export and client workflows

Every existing mailing client API operation has an admin counterpart. Replace
`/api/` with `/api/admin/clients/{client_id}/` and use an admin key. The selected
client is explicit in the path; payload audience/client slugs still follow the
existing scope validation. These endpoints reuse the existing implementation,
including validation, dry runs, CSV handling and import result counts.

```bash
curl "$RELAY_URL/api/admin/clients/1/contacts/imports" \
  -H "Authorization: Bearer $RELAY_ADMIN_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"audience":"dtc-courses","client":"dtc-courses","dry_run":true,"contacts":[{"email":"person@example.com","status":"subscribed"}]}'

curl "$RELAY_URL/api/admin/clients/1/contacts.csv?audience=dtc-courses&client=dtc-courses" \
  -H "Authorization: Bearer $RELAY_ADMIN_API_KEY" \
  -o contacts.csv
```

CSV imports use `clients/{id}/contacts/imports/csv`, with the same JSON `csv`
string or multipart `file` upload as the client endpoint. This namespace also
covers recipient lists, transactional sends/templates, campaign workflows,
contact history and Mailchimp settings. See [client API contracts](api.md).

## Operator interface parity

Operator reads and actions have JSON counterparts. The API uses explicit client
IDs rather than the browser's selected-client session. Presentation concerns
(forms, redirects, badges and HTML pages) are represented by data and actions.

| Operator capability | Admin API counterpart |
| --- | --- |
| Dashboard and worker health | GET `dashboard` (optional `client_id`), GET `workers` |
| Client, audience and tag management | Management resources above |
| Draft campaign creation/editing | POST `clients/{id}/campaign-drafts`; PATCH `clients/{id}/campaign-drafts/{campaign_id}` |
| Campaign queue estimate and queueing | GET `campaigns/{id}/queue`; POST with `{"confirm":true}` |
| Resolve failed recipient as sent | POST `campaigns/{id}/recipients/{recipient_id}/assume-sent` |
| Contact verification, validation and suppression | POST `contacts/{id}/state`, using `ContactStateForm` fields |
| Contact subscription updates | POST `clients/{id}/contacts/{contact_id}/subscriptions`, using operator form fields |
| Add contact tag, including creating a new tag | POST `clients/{id}/contacts/{contact_id}/tags/add`, using operator form fields |
| Remove contact tag | POST `clients/{id}/contacts/{contact_id}/tags/remove`, with `tag_id` |
| Mailchimp tag mappings | GET/PUT `clients/{id}/client/mailchimp/tag-mappings` |
| Retire/reactivate inbound address | PATCH `inbound-addresses/{id}`, with boolean `is_active` |
| Mark inbound message read | POST `inbound-messages/{id}/mark-read` |
| Block sender/domain | POST `inbound-messages/{id}/block`, with `scope` = `address` or `domain` |
| Unblock message sender | POST `inbound-messages/{id}/unblock` |
| Inspect/retry dead letters | GET `dead-letters`; POST `dead-letters/{uuid}/retry` |
| Manage admin credentials | `/admin-api-keys/` in UI; admin `api-keys` routes |

Read collections and `/{id}` detail endpoints are available for `contacts`,
`subscriptions`, `contact-tags`, `campaigns`, `campaign-recipients`, `templates`,
`transactional-messages`, `events`, `audits`, `client-callbacks`, `cmp-callbacks`,
`inbound-messages`, `inbound-addresses`, `blocked-senders` and `dead-letters`.
Inbound message detail includes the body; marking it read is an explicit POST.
Related histories and subscriptions can be fetched through the filtered read
collections. GET `contacts/{id}/overview`, `audiences/{id}/overview` and
`campaigns/{id}/overview` expose the UI metrics, eligibility, breakdowns and send
progress; pass optional `client_id` for a selected-client view. Template authoring/publishing and other client workflows use their
scoped counterparts above. Draft campaign fields match the UI, including
`audience`, `client`, `subject`, `preview_text`, `html_body`, `text_body`,
`scheduled_at`, and arrays of tag IDs for `include_tags`/`exclude_tags`.

Contact state accepts `verified_state` (`unchanged`, `verified`, `unverified`),
`email_validation_status`, optional `email_validation_reason`, and booleans
`global_unsubscribed`, `hard_bounced`, `complained`. Subscription updates accept
`audience`, `status`, optional `verified` and `unsubscribe_reason`; the path
selects the client. Tag addition accepts `audience`, optional existing `tag`, or
`new_tag_name`/`new_tag_slug` to create one.

`OPERATOR_API_PARITY` in `mailing/admin_urls.py` maps staff UI capabilities to API
routes. Tests fail when a staff UI view is added without a counterpart. A second
test ensures each mailing client API route has an admin route. These checks
protect route coverage; service integration tests check shared behavior.

## Errors

Errors are JSON objects with an `error.code` and, for validation failures,
`error.fields`. Invalid/revoked credentials return 401; invalid JSON, types or
field values return 400; missing resources return 404; unsupported methods return
405; non-JSON management writes return 415; database uniqueness races return
409. Existing scoped workflows preserve the client API's error contracts.

## Jobs and schedule operations

Management keys can inspect `GET jobs`, `GET jobs/{uuid}`, `GET schedules`, and
`GET schedules/{uuid}`. Lists accept `client_id`; responses omit task payloads.
These are shared-service resources, so their client is explicit on each record.

`POST jobs/{uuid}/retry` and `POST schedules/{uuid}/action` use the same locked,
confirmed operations as the staff interface. Supply `client_id`, the record's
exact `updated_at` string as `revision`, and JSON `confirmed: true`. Schedule
operations also require `action: "pause"` or `action: "resume"`. Incomplete,
stale, or ineligible operations return HTTP 409. Only failed jobs can be retried.
A retry may repeat a receiver side effect if a completion callback was lost.

Pausing stops future submissions; queued or running jobs continue. Resuming sets
the next future cron occurrence without submitting work immediately or replaying
missed occurrences. Automatic campaign scheduling remains unavailable; these
schedules describe Relay task submissions.
