# Campaign link click statistics

Campaign link statistics are available from the client API and Relay CLI. The API
key only sees campaigns owned by its client.

## API

```text
GET /api/campaigns/{external_key}/links
Authorization: Bearer <client-api-key>
```

Example response:

```json
{
  "campaign": "cmp-registration-production-ai-agents-generative-media-10ac3865",
  "links": [
    {
      "url": "https://example.com/course",
      "total_clicks": 18,
      "unique_clickers": 12
    }
  ]
}
```

Counts come from recorded campaign `click` events with a destination URL. Total
clicks count every tracked event; unique clickers count each campaign recipient
once for that URL. Automated scanners can create tracked clicks.

Unknown campaign keys return a `404` validation error. Requests require a client
API key and return only campaigns belonging to that key's client.

## CLI

```bash
relay campaigns links <external-key>
relay campaigns links <external-key> --json
```

The CLI uses the configured Relay URL and client API key. It fails visibly when the
API returns an error or the request exceeds the CLI's 30-second timeout.

## Operator dashboard

The campaign detail page shows a **Clicks per link** table with total clicks and
unique clickers beside the campaign's aggregate engagement counts. The table
loads in the background and reports a visible retryable error if the request fails
or takes more than 10 seconds.
