# datamailer

Command-line client for sending email through a [Datamailer](https://github.com/DataTalksClub/datamailer)
deployment. Built for the "an agent/script finished — just email me the result" case:
no need to push to GitHub or open a dashboard, just pipe the output into an email.

It is a thin, dependency-free wrapper over Datamailer's transactional API. It works
against any Datamailer instance — yours or someone else's — configured with a URL and a
client API key.

## Install

```bash
pip install datamailer
# or, run without installing:
uvx datamailer --help
```

Requires Python 3.11+.

## Configure

A Datamailer operator issues you a client API key (`dm_...`) and tells you the URL where
their instance is deployed. Then:

```bash
datamailer configure --url https://datamailer.example.com --api-key dm_xxx
# optionally set a default recipient and sender:
datamailer configure --default-to me@example.com --default-from results
```

Settings are stored in `~/.config/datamailer/config.toml` (mode `0600`). You can override
or skip the file entirely with the environment variables `DATAMAILER_URL` and
`DATAMAILER_API_KEY`, or per-command with `--url` / `--api-key`.

Verify the connection:

```bash
datamailer whoami
```

## Send

```bash
# inline body
datamailer send --to me@example.com --subject "Pipeline done" --body "All 42 jobs passed."

# body from stdin — ideal for agents and scripts
./run_pipeline.sh | datamailer send --to me@example.com --subject "Pipeline output"

# body from a file, as HTML
datamailer send --to me@example.com -s "Report" --body-file report.html --html
```

The first send against a deployment auto-creates a generic transactional template
(`cli-message`, or `cli-message-html` for `--html`) on your client, then reuses it. Your
subject and body are passed as the template context.

Other commands:

```bash
datamailer status <message_id>     # delivery status + event timeline
datamailer senders                 # list configured sender addresses
datamailer senders --set 'results=Agent <agent@example.com>' --default results
```

Add `--json` to `send`, `status`, `whoami`, or `senders` for machine-readable output.

## Notes

- Sends require the client to have a verified sender address configured. If `whoami`
  shows no sender, set one with `datamailer senders --set ...` (the address must be
  verified in the deployment's SES).
- Transactional sends are blocked for recipients that hard-bounced or complained.

### Sending domains and sender maintenance

Operators register a sending domain through Relay using an admin API key (set
`DATAMAILER_API_KEY` privately; do not pass credentials in shell history):

```bash
datamailer domains --admin --data-file domain.json
datamailer domains --admin --id 1 --verify
datamailer domains
```

Example `domain.json` (provider route identifiers are not credentials):

```json
{
  "name": "dtcdev.click",
  "organization_id": 1,
  "client_ids": [1],
  "role_arn": "arn:aws:iam::817685572750:role/relay-sandbox-email-send",
  "region": "us-east-1",
  "configuration_set": "datamailer-sandbox"
}
```

Use actual organization/client IDs. An empty `client_ids` list grants the domain
to the whole organization. Only an admin can authorize ownership and provider
routes; a client key can list its authorized domains. Relay registers/reads the
identity through its provider, returns verification, DKIM, MAIL FROM, DMARC and
required DNS records. Publish the returned DNS records with your DNS operator,
then verify again. Existing domain identities and mail-from settings are
preserved. A provider access error is returned as unavailable status, never a
successful verification. Provider IAM permissions must already cover the domain;
callers do not need AWS credentials or console access.

`--data-file` with `--id` updates the route; `--delete --id` disables it and
prevents fallback to another provider. The same controls are available at
`/api/admin/sending-domains` and in Django admin under Sending domains.

Sender changes merge with the current policy, preserving other sender IDs:

```bash
datamailer senders --set 'alerts=DTC Alerts <alerts@dtcdev.click>'
datamailer senders --default alerts
datamailer senders --delete obsolete-sender
```

New addresses require an authorized, provider-verified and routed domain through
both the API and admin forms. Previously configured addresses without a domain
record are retained for migration compatibility; registering their domain
activates verification on subsequent policy changes. Removing the default sender
requires choosing another default in the same command. The underlying PUT API
still replaces the complete sender policy, so API callers must preserve entries
that they are not changing. Recipient configuration and inbound allowlists are
independent and never changed by these commands.
