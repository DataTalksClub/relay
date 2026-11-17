# Lead capture pages (materials.dtcdev.click)

A page per talk or workshop where people leave their email for the materials,
for example `https://materials.dtcdev.click/<event-slug>`. Show its QR code on a
slide and say "scan this and I'll send you the slides".

## What happens

1. The visitor enters their email, optionally a name, and ticks the required box
   "I agree to be contacted by Alexey Grigorev". The
   button stays disabled until the box is ticked, and the server rejects a
   submit without it.
2. Relay stores the contact in the `talk-leads` audience (client `talk-leads`,
   organization `datatalksclub`) as **pending**. It records the consent text,
   the time, IP, user agent and event in `ContactSourceMetadata` with source
   `lead:<event-slug>`. It then sends one confirmation email (template
   `lead-confirm`, from `Alexey Grigorev <alexey@datatalks.club>`).
3. The link in the email opens a page with a "Confirm and get the materials"
   button. Confirmation is a POST, so link scanners can't confirm on someone's
   behalf. On confirm the contact becomes **subscribed** and verified and gets
   the tag `<event-slug>`, and the page shows the download. For events with a thank-you template, a
   second email contains the signed PDF link and follow-up message. Repeat
   confirmation sends no duplicate. Already verified subscribers to talk-leads
   skip confirmation when signing up for another event: the new consent and
   event tag are recorded, the materials open immediately, and the event
   thank-you email is sent once. Signed links work for 365 days.
4. Later, send the slides as a normal Relay campaign: client `talk-leads`,
   audience `talk-leads`, **include tag** `<event-slug>`. Only confirmed signups
   carry the tag.

Abuse handling: a hidden honeypot field (`company`) gets a fake success; a
second submit within 10 minutes sends nothing (idempotent); at most 3
confirmation emails per address per hour; 120 signups per IP per hour (a talk
room shares one NAT address). The relay.datatalks.club WAF applies on top. Every
confirmation has a fresh signed token, so its rendered content is unique and
the duplicate-content guard never blocks a legitimate resend. Bounced or
complained addresses get the same "check your inbox" page and no email.

## Create a page for a new event

Events, copy and downloadable files are data, not code, so no deploy is needed.

**Django admin** (`https://relay.datatalks.club/admin/`, SSO):

- *Lead materials* → Add: slug, title, file name, upload the PDF. Files are
  stored in Relay's Postgres and served by Relay, never from GitHub.
- *Lead events* → Add: slug (becomes the URL), title, intro, offer line,
  material (or an external `material_url`), consent text, active. The change page
  shows the public link, the QR code (SVG/PNG download links) and signup counts.

**CLI** (inside a Relay prod container, e.g. the scheduler):

```bash
python manage.py lead_event material 28-prompts --title "From Idea to Production: The 28 Prompts Working Kit" \
    --filename from-idea-to-production-28-prompts.pdf --file -  < kit.pdf
python manage.py lead_event set my-talk --title "My talk" \
    --intro "Leave your email and I'll send you the slides after the talk." \
    --offer "Right now: the 28-prompts working kit (PDF)." --material 28-prompts
python manage.py lead_event qr my-talk --out /tmp/qr     # my-talk-qr.svg, my-talk-qr.png
python manage.py lead_event list                         # signups / confirmed per event
python manage.py lead_event set my-talk --inactive       # close signups
```

QR codes are also served live at `https://materials.dtcdev.click/<slug>/qr.svg`
and `/qr.png`.

The confirmation email is the Relay transactional template `lead-confirm` of the
`talk-leads` client. Edit it in Relay. `manage.py provision_lead_capture`
creates the client, audience, sender and template once and never overwrites an
edited template unless you pass `--update-template`.

## Hosting

`materials.dtcdev.click` is a CloudFront distribution in the sandbox account
(`817685572750`, id `E2CWZ8DS4QLEO7`, ACM cert
`b0966f9a-2874-4d85-979b-ef4f791b5e48`, Route53 zone `Z05963572WVWFHDQZH5NE`).
Its origin is `relay.datatalks.club` with origin path `/m`, caching disabled,
and the viewer Host header is not forwarded (`AllViewerExceptHostHeader`). So the
production Relay edge, its WAF and its origin secret apply unchanged, and no
Coolify routing or Terraform-managed edge was touched. A viewer-request CloudFront
Function (`relay-materials-client-ip`) overwrites `X-Materials-Client-Ip` with
the visitor address, because behind two CloudFront hops `X-Forwarded-For` starts
with an edge address. Relay serves the pages at `/m/<slug>`; `RELAY_LEADS_BASE_URL` (default
`https://materials.dtcdev.click`) builds the links in emails and QR codes.

Settings: `RELAY_LEADS_ORG`, `RELAY_LEADS_CLIENT`, `RELAY_LEADS_AUDIENCE`,
`RELAY_LEADS_CONFIRM_TEMPLATE`, `RELAY_LEADS_RESEND_SECONDS`,
`RELAY_LEADS_EMAIL_LIMIT`/`_WINDOW_SECONDS`, `RELAY_LEADS_IP_LIMIT`/`_WINDOW_SECONDS`,
`RELAY_LEADS_TOKEN_MAX_AGE_SECONDS`, `RELAY_LEADS_MAX_MATERIAL_BYTES`
(see `relay/settings.py`).

## Optional email after confirmation

Set an event's **thank_you_template_key** in Lead events admin, or use
`uv run python manage.py lead_event set <slug> --thank-you-template-key lead-thank-you`.
Blank leaves the event's existing download-only behavior unchanged.
`uv run python manage.py provision_lead_capture` creates `lead-thank-you` in the
`talk-leads` client and preserves any edits to existing templates.

Edit that transactional template in Relay, or create a different template per
event and select its key. Context: `event_title`, `download_url` (the signed PDF
link), and `name`. Wording and template selection take effect without a deploy.
The default thanks the attendee, links the guide, promises materials within a
few days, and links Alexey's LinkedIn and Substack. It uses the client sender
Alexey Grigorev <alexey@datatalks.club>.

The email is queued on confirmation POST or an already verified subscriber's
signup, in the same transaction as the confirmation receipt. The per-signup row lock and stored message ID prevent
repeat confirms (including different confirmation tokens) from sending again.
Events with no selected template do not send this email. Hard suppression still
applies; a suppressed email does not block the promised download.

Confirmation requests time out visibly in the browser after 12 seconds and
restore the button for a safe retry. Server failures are logged and return a
503 page with a retry button. Campaign sends append Relay's unsubscribe link
to both HTML and text and include one-click unsubscribe headers.
