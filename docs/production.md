# Relay production

Relay production is live at `https://relay.datatalks.club` on the Contabo
Coolify deployment. The active database is `relay`; the production Coolify
resource is named `relay-staging` for historical reasons. The supported
delivery path and operational checks are documented in
[Relay deployment](relay-deployment.md).

The old EC2, RDS, SSM, and CloudWatch design described in previous versions of
this file was a plan and is not the running Relay deployment. Datamailer
remains a separate sandbox project; its future production plans are not Relay
production instructions.
