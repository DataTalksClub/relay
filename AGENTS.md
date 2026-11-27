### Use UV for Python Package Management

When installing Python packages, use `uv` instead of `pip`.

Wrong:

```bash
pip install djangorestframework
```

Right:

```bash
uv add djangorestframework
```

Run Django commands with `uv run`:

```bash
uv run python manage.py migrate
uv run python manage.py test
```

### Deployment Environments

Relay has a production deployment (relay.datatalks.club) that sends the
DataTalks.Club weekly newsletter and transactional email for other services.
Treat it as production: real subscribers, real SES reputation, real cost.

### No Load Tests Against Production or Real Mailboxes

Never run load, capacity or throughput tests by sending real email through
production SES. This includes "synthetic" campaigns to our own domains
(`dtcdev.click`, `datatalks.club` or any `+tag` alias on them).

Why: on 2026-11-16 a capacity test sent about 60,000 synthetic emails from
production Relay to `dev+...@dtcdev.click`. Mail to that domain is received
by Dapier, which stored and processed every message before refusing it. That
caused tens of millions of DynamoDB reads and writes, about $7 of unplanned
cost in one day, 60,000 junk rows in Dapier's inbox table, and sends that
count against our SES quota and reputation.

For performance or capacity work:

- Use the fake SES backend or `RELAY_DRY_RUN` locally or in a test
  environment, so nothing reaches SES.
- If real SES behaviour must be checked, use the SES mailbox simulator
  (`success@simulator.amazonses.com`) with a small number of messages, never
  thousands.
- Never send to `dtcdev.click` or `datatalks.club` addresses in bulk. Those
  domains have inbound processing (Dapier) that turns every message into work
  and cost.
- Any production campaign with more than a handful of recipients that is not
  the real newsletter or a real course email needs explicit approval from a
  human first.
