# datamailer-next sandbox deployment

The taskdeck-integrated rebuild, deployed separately from the live host so a
cutover is a deliberate DNS change rather than a side effect.

| | |
|---|---|
| URL | https://datamailer-next.dtcdev.click |
| Instance | `i-0ce6f1e52e72b03df` (t4g.small, arm64, us-east-1a) |
| Branch | `taskdeck-integration` |
| Access | SSM Session Manager; no SSH key |
| TLS | Caddy, Let's Encrypt, renewed automatically |

The live `datamailer.dtcdev.click` (`i-044716a7d6b053eef`) is untouched and
still serves CMP production.

## Layout

One image, four containers: `db` (Postgres 17), `web` (gunicorn), `worker`
(`manage.py db_worker`), `caddy`. The `migrate` service runs once and web waits
on it completing.

The worker is a separate container rather than a thread inside web so a large
campaign send cannot starve request handling, and so `docker compose logs
worker` answers "what is the queue doing" without filtering.

The two ingress drains (`ingress-ses`, `ingress-inbound`) are behind the
`ingress` compose profile and are not started, because this host has no SES
queue URLs configured. Start them with `--profile ingress` once it does.

## Operating it

```bash
cd /opt/datamailer
docker compose --env-file .env.prod -f docker-compose.prod.yml ps
docker compose --env-file .env.prod -f docker-compose.prod.yml logs -f worker
```

`datamailer.service` brings the stack up on boot.

To deploy a change: `git pull`, then `up -d --build`.

## Known limitations

The public IP is not an Elastic IP, so a stop/start changes it and the A record
needs updating. This matches how the existing sandbox host is set up; worth
fixing on both if either becomes long-lived.

`SES_MAX_SEND_RATE` is enforced by a process-local lock, so running more than
one worker container multiplies the effective rate. Keep the worker at one
replica until that constraint moves somewhere shared.

Secrets were generated at launch and live only in `/opt/datamailer/.env.prod`
(mode 600) and in the instance user-data. Rotate them if this host outlives its
sandbox purpose.
