# Data Model

The model separates global contact identity from audience/client subscription state. This matters because the same email can belong to DataTalksClub and AI Shipping Labs with different permissions and preferences.

## Core Tables

### organizations

Represents the top-level owner.

Examples:

- `datatalksclub`
- `ai-shipping-labs`

Important fields:

- `id`
- `name`
- `slug`
- `created_at`

### audiences

Represents a list or brand-level audience.

Examples:

- `datatalks-club`
- `ai-shipping-labs`

Important fields:

- `id`
- `organization_id`
- `name`
- `slug`
- `created_at`

### clients

Represents an application or product using Datamailer.

Examples:

- `dtc-newsletter`
- `dtc-courses`
- `ai-shipping-labs`

Important fields:

- `id`
- `organization_id`
- `name`
- `slug`
- `is_active`
- `created_at`

### client_api_keys

Represents named Bearer credentials for client integrations.

Important fields:

- `id`
- `client_id`
- `name`
- `key_hash`
- `public_id`
- `notes`
- `last_used_at`
- `revoked_at`
- `created_at`
- `updated_at`

Only hashes are stored. `public_id` is safe to display as `dm_<public_id>` in the operator UI, API docs, and audit metadata. `revoked_at IS NULL` means active, and active key names are unique per client.

### contacts

Global email identity.

Important fields:

- `id`
- `email`
- `normalized_email`
- `verified_at`
- `global_unsubscribed_at`
- `hard_bounced_at`
- `complained_at`
- `created_at`
- `updated_at`

Constraints and indexes:

- Unique `normalized_email`.
- Index `verified_at`.
- Index `global_unsubscribed_at`.

### subscriptions

Subscription state scoped to an audience and optionally a client.

Important fields:

- `id`
- `contact_id`
- `audience_id`
- `client_id`
- `status`: `pending`, `subscribed`, `unsubscribed`
- `verified_at`
- `unsubscribed_at`
- `unsubscribe_reason`
- `created_at`
- `updated_at`

Constraints and indexes:

- Unique `(contact_id, audience_id, client_id)`.
- Index `(audience_id, client_id, status)`.
- Index `(contact_id, updated_at)`.

### tags

Reusable labels within an audience.

Important fields:

- `id`
- `audience_id`
- `name`
- `slug`

Constraints and indexes:

- Unique `(audience_id, slug)`.

### contact_tags

Many-to-many membership between contacts and tags.

Important fields:

- `contact_id`
- `tag_id`
- `created_at`

Constraints and indexes:

- Unique `(contact_id, tag_id)`.
- Index `(tag_id, contact_id)`.

## Campaign Tables

### campaigns

Campaign definition and aggregate stats.

Important fields:

- `id`
- `client_id`
- `audience_id`
- `subject`
- `preview_text`
- `html_body`
- `text_body`
- `status`: `draft`, `queued`, `snapshotting`, `sending`, `sent`, `cancelled`, `failed`
- `scheduled_at`
- `sent_at`
- `include_tags`
- `exclude_tags`
- `recipient_count`
- `sent_count`
- `skipped_count`
- `delivered_count`
- `unique_open_count`
- `open_count`
- `unique_click_count`
- `click_count`
- `unsubscribe_count`
- `bounce_count`
- `complaint_count`
- `created_at`
- `updated_at`

Derived rates:

- Open rate: `unique_open_count / sent_count`.
- Click rate: `unique_click_count / sent_count`.
- Click-to-open rate: `unique_click_count / unique_open_count`.
- Bounce rate: `bounce_count / sent_count`.
- Unsubscribe rate: `unsubscribe_count / sent_count`.

### campaign_recipients

One row per intended campaign recipient.

For a 120k-recipient campaign, this table gets 120k rows. This is intentional. It provides auditability and supports send/not-send status.

Important fields:

- `id`
- `campaign_id`
- `contact_id`
- `email`
- `status`: `pending`, `sent`, `skipped`, `failed`, `bounced`, `complained`, `unsubscribed`
- `skip_reason`: `unverified`, `global_unsubscribe`, `client_unsubscribe`, `audience_unsubscribe`, `hard_bounce`, `complaint`, `duplicate`, `suppressed`
- `tracking_token_hash`
- `unsubscribe_token_hash`
- `ses_message_id`
- `sent_at`
- `delivered_at`
- `first_opened_at`
- `first_clicked_at`
- `open_count`
- `click_count`
- `last_error`
- `created_at`
- `updated_at`

Constraints and indexes:

- Unique `(campaign_id, contact_id)`.
- Unique `tracking_token_hash`.
- Unique `unsubscribe_token_hash`.
- Index `(campaign_id, status)`.
- Index `(contact_id, sent_at)`.
- Index `ses_message_id`.
- Index `first_opened_at`.
- Index `first_clicked_at`.

### email_events

Append-only event timeline.

Important fields:

- `id`
- `campaign_id`
- `campaign_recipient_id`
- `transactional_message_id`
- `contact_id`
- `client_id`
- `audience_id`
- `event_type`: `queued`, `skipped`, `sent`, `delivered`, `open`, `click`, `unsubscribe`, `bounce`, `complaint`, `failed`
- `url`
- `metadata`
- `created_at`

Indexes:

- `(contact_id, created_at)`.
- `(campaign_id, event_type, created_at)`.
- `(campaign_recipient_id, event_type)`.
- `(client_id, created_at)`.

Growth plan:

- Keep this append-only.
- Partition monthly when volume grows enough to make maintenance/reporting painful.
- Archive old raw events to S3 if needed.

## Transactional Email Tables

### email_templates

The editable draft of one reusable transactional or campaign template. Sends never render from this row directly; they render published `template_versions` snapshots.

Important fields:

- `id`
- `client_id`
- `key`
- `name`
- `subject`
- `content_format`: `html` or `markdown`
- `html_body`
- `text_body`
- `markdown_body`: canonical markdown source when `content_format` is `markdown`
- `category`: optional template metadata carried onto published versions
- `required_context`
- `example_context`
- `is_transactional`
- `created_at`
- `updated_at`

Constraints and indexes:

- Unique `(client_id, key)`.

### template_versions

Immutable published snapshots of an `email_templates` draft. Publishing writes version N+1 (1-based per template); republishing unchanged content returns the latest existing row. Rows are write-once: no API, management command, or admin action modifies a published version.

Important fields:

- `id`
- `template_id`
- `version`: 1-based per template
- `subject`
- `content_format`: `html` or `markdown`
- `html_body`
- `text_body`
- `markdown_body`
- `category`
- `required_context`: normalized `[{name, description}]`
- `published_at`

Constraints and indexes:

- Unique `(template_id, version)`.
- Index `(template_id, version)` supports latest-version resolution for sends.

### transactional_messages

One row per transactional send request.

Important fields:

- `id`
- `client_id`
- `contact_id`
- `email`
- `template_id`
- `template_key`
- `template_version`: resolved published version number at send time
- `status`: `queued`, `sent`, `failed`, `skipped`, `bounced`, `complained`
- `idempotency_key`
- `ses_message_id`
- `sent_at`
- `delivered_at`
- `first_opened_at`
- `first_clicked_at`
- `open_count`
- `click_count`
- `metadata`
- `last_error`
- `created_at`
- `updated_at`

Constraints and indexes:

- Unique `(client_id, idempotency_key)` when idempotency key is present.
- Index `(contact_id, created_at)`.
- Index `(client_id, status, created_at)`.
- Index `ses_message_id`.

## Contact History

Contact history is assembled from:

- Contact creation and verification fields.
- Subscription changes.
- Campaign recipient rows.
- Transactional message rows.
- Email events.

The UI should show this as a chronological timeline per contact.
