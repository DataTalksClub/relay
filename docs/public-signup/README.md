# Public website signup (single opt-in)

Static sites post to `POST /api/public/lists/<key>/subscribe` with no API key.
A list is one entry of `RELAY_PUBLIC_LISTS`:

```text
dtc-newsletter org=datatalksclub client=dtc-newsletter audience=dtc category=newsletter template=newsletter-welcome optin=single
```

`optin=single` (the Mailchimp behaviour datatalks.club had) subscribes on
submit and sends `template` as a transactional welcome email. It needs no
`confirm_base`. Without `optin`, the list is double opt-in as before.

Request body: `{"email": "...", "page": "<optional page URL>", "company": ""}`.
`company` is the honeypot: a hidden field real people leave empty. A filled one
returns the normal `{"status":"subscribed"}` and does nothing.

Responses (HTTP 200 unless noted): `subscribed`, `already_subscribed` (no
email sent), `not_resubscribed` (the address unsubscribed, bounced or
complained earlier; an anonymous form never resubscribes it). 400
`validation_error`, 429 `rate_limited` (per email, per IP), 404/503 when the
list or template is not set up, 500 `internal_error`.

## Protection and consent

- Per email: `RELAY_PUBLIC_SUBSCRIBE_EMAIL_LIMIT` per
  `RELAY_PUBLIC_SUBSCRIBE_EMAIL_WINDOW_SECONDS` (3 per hour).
- Per IP: `RELAY_PUBLIC_SUBSCRIBE_IP_LIMIT` per
  `RELAY_PUBLIC_SUBSCRIBE_IP_WINDOW_SECONDS` (10 per hour). The IP is the
  `RELAY_PUBLIC_SUBSCRIBE_TRUSTED_PROXY_HOPS`-th entry from the right of
  `X-Forwarded-For` (default 1).
- `RELAY_PUBLIC_SUBSCRIBE_ORIGINS` lists the browser origins allowed by CORS.
- Consent evidence is stored in `ContactSourceMetadata` (source
  `website-signup`): time, IP, origin, user agent, page, list key. The
  subscription is not marked verified.

## Failures are loud

Unexpected exceptions and 404/5xx outcomes log `relay_public_subscribe_failed`.
Once the client is known, every call is counted by the transactional monitor
(`TransactionalRequestMinute`), so 5xx and error floods page like the client
API. If the welcome email cannot be queued, the subscription is rolled back
and the visitor sees an error, so nobody is subscribed without being told.

## Template

The welcome email is a normal transactional template, edited in Relay:
`GET/POST /api/transactional/templates`. `dtc-newsletter-welcome.html` and
`.txt` here are the text of the Mailchimp "Welcome DataTalks.Club (form)"
automation email ("Thanks for joining us", Slack invite
http://join.datatalks.club/). Only Mailchimp merge tags changed: subject and
preview text filled in, and the "view in browser", "update preferences" and
"unsubscribe" links removed (transactional mail has no per-recipient
Mailchimp links); the footer now says why the one-time email was sent.
