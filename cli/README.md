# Relay CLI

Command-line client for managing campaigns, contacts, templates, and sends through Relay.
The `relay` executable is the primary command. `datamailer` remains as an alias for
existing transactional send workflows.

It is a thin, dependency-free wrapper over Relay's operator and client APIs. The
legacy `datamailer` executable and `DATAMAILER_*` config names remain available
for existing send workflows.

## Install

```bash
uv tool install ./cli
relay --help
```

Requires Python 3.11+.

## Login

Relay uses the DataTalksClub shared device login library for the same short-code
and browser approval flow as dapier. The shared package must be available on
`PYTHONPATH` or through `DTC_AUTH_HOME` (the repository checkout is discovered
automatically when `auth` is a sibling directory):

```bash
relay auth login --url https://relay.example.com
relay auth status
relay auth logout
```

The login command waits for at most 15 minutes by default. Use `--timeout` to
change that limit. Credentials are stored with mode `0600` in the XDG config
file and are never printed. Logout revokes the Relay admin API key when Relay
is reachable, then removes the local credential.

## Configure

For operator commands, use `relay auth login` to create a Relay admin key. For
client-scoped transactional commands, use a client API key issued by a Relay
operator. Configure stores one credential at a time:

```bash
relay configure --url https://relay.example.com --api-key relay_xxx
relay configure --default-to me@example.com --default-from results
```

Settings are stored in `~/.config/relay/config.toml` (mode `0600`). You can override
them with `RELAY_URL` and `RELAY_API_KEY`, or per-command with `--url` / `--api-key`.
Legacy `DATAMAILER_*` variables and config files remain supported.

Verify the connection:

```bash
relay whoami
```

## Send

```bash
# inline body
relay send --to me@example.com --subject "Pipeline done" --body "All 42 jobs passed."

# body from stdin — ideal for agents and scripts
./run_pipeline.sh | relay send --to me@example.com --subject "Pipeline output"

# body from a file, as HTML
relay send --to me@example.com -s "Report" --body-file report.html --html
```

The first send against a deployment auto-creates a generic transactional template
(`cli-message`, or `cli-message-html` for `--html`) on your client, then reuses it. Your
subject and body are passed as the template context.

Other commands:

```bash
relay status <message_id>     # delivery status + event timeline
relay senders                 # list configured sender addresses
relay senders --set 'results=Agent <agent@example.com>' --default results
```

Campaign link clicks are available to a client API key without database access:

```bash
relay campaigns links cmp-registration-production-ai-agents-generative-media-10ac3865
```

The command groups tracked click events by destination URL and prints total clicks
and distinct campaign recipients. Add `--json` for machine-readable output. API
transport failures and the 30-second request timeout are reported on stderr.

Operator API commands:

```bash
relay operator campaigns list
relay operator campaigns show --id 12
relay operator campaigns stats --id 12
relay operator campaigns create --client-id 3 --data-file draft.json
relay operator campaigns queue --id 12 --confirm
relay operator campaigns recipients --client-id 3 --client-slug website --audience weekly --key issue-42
relay operator contacts list
relay operator complaints list
relay operator contacts history --client-id 3 --id 27
relay operator lists list
relay operator lists members --client-id 3 --client-slug website --audience weekly --key weekly-recipients
relay operator newsletters list --client-id 3
relay operator newsletters show --client-id 3 --key november-2026
relay operator templates versions --client-id 3 --key weekly-digest
relay operator messages list
relay operator messages retry --id 71 --confirm
relay operator messages bulk-retry --data-file retry-filters.json --confirm
relay operator stats list
relay operator archive export --resource-name contacts --offset 0 --limit 200
```

`relay operator` actions use a Relay admin API key. Campaign queueing requires
an explicit `--confirm`; bulk retry without it only runs when the JSON file sets
`"dry_run": true`. `relay api METHOD /api/...` is available for API
operations that do not yet have a dedicated command. Current UI/API/CLI gaps
are tracked in [`docs/relay-cli-coverage.md`](../docs/relay-cli-coverage.md).

Add `--json` to `send`, `status`, `whoami`, or `senders` for machine-readable output.

## Notes

- Sends require the client to have a verified sender address configured. If `whoami`
  shows no sender, set one with `relay senders --set ...` (the address must be
  verified in the deployment's SES).
- Transactional sends are blocked for recipients that hard-bounced or complained.

### Sending domains and sender maintenance

Operators register a sending domain through Relay using an admin API key (set
`RELAY_API_KEY` privately; do not pass credentials in shell history):

```bash
relay domains --admin --data-file domain.json
relay domains --admin --id 1 --verify
relay domains
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
relay senders --set 'alerts=DTC Alerts <alerts@dtcdev.click>'
relay senders --default alerts
relay senders --delete obsolete-sender
```

New addresses require an authorized, provider-verified and routed domain through
both the API and admin forms. Previously configured addresses without a domain
record are retained for migration compatibility; registering their domain
activates verification on subsequent policy changes. Removing the default sender
requires choosing another default in the same command. The underlying PUT API
still replaces the complete sender policy, so API callers must preserve entries
that they are not changing. Recipient configuration and inbound allowlists are
independent and never changed by these commands.
