#!/usr/bin/env bash
set -euo pipefail

# Run on the existing Relay host. Credentials never enter command output.
if [[ "${1:-}" == --dry-run ]]; then
  echo 'Provision AISL: organization=aisl audience=aisl client=aisl-website'
  echo 'Use the running relay-web image and /etc/relay/{infrastructure,runtime,aisl}.env'
  echo 'Generate a separate AISL API key and webhook secret only if absent (mode 0600).'
  echo 'Register https://aishippinglabs.com as the permitted job webhook origin.'
  echo 'Configure noreply, content, welcome and team senders at aishippinglabs.com.'
  exit 0
fi
if [[ $# -ne 0 ]]; then
  echo "usage: $0 [--dry-run]" >&2
  exit 2
fi

readonly credential_file=/etc/relay/aisl.env
readonly runtime_file=/etc/relay/runtime.env
readonly infrastructure_file=/etc/relay/infrastructure.env
for required_file in "$runtime_file" "$infrastructure_file"; do
  [[ -f "$required_file" ]] || { echo "Missing $required_file" >&2; exit 1; }
done
image="$(docker inspect --format '{{.Config.Image}}' relay-web)"
docker image inspect "$image" >/dev/null

umask 077
touch "$credential_file"
chmod 0600 "$credential_file"
if ! grep -q '^RELAY_AISL_API_KEY=' "$credential_file"; then
  printf 'RELAY_AISL_API_KEY=relay_aisl%s_%s\n' \
    "$(openssl rand -hex 8)" "$(openssl rand -hex 32)" >>"$credential_file"
fi
if ! grep -q '^RELAY_AISL_WEBHOOK_SECRET=' "$credential_file"; then
  printf 'RELAY_AISL_WEBHOOK_SECRET=%s\n' "$(openssl rand -hex 32)" >>"$credential_file"
fi

run_app() {
  docker run --rm -i --network host \
    --env-file "$infrastructure_file" --env-file "$runtime_file" \
    --env-file "$credential_file" "$image" "$@"
}

run_app python manage.py provision_client_scope \
  --organization aisl --organization-name 'AI Shipping Labs' \
  --audience aisl --audience-name 'AI Shipping Labs' \
  --client aisl-website --client-name 'AI Shipping Labs Website'
run_app python manage.py provision_client_api_key \
  --organization aisl --client aisl-website --name aisl-deployment \
  --raw-key-env RELAY_AISL_API_KEY
run_app python manage.py set_client_senders aisl-website \
  --organization aisl --default-sender noreply \
  --sender 'noreply=AI Shipping Labs <noreply@aishippinglabs.com>' \
  --sender 'content=AI Shipping Labs <content@aishippinglabs.com>' \
  --sender 'welcome=AI Shipping Labs <welcome@aishippinglabs.com>' \
  --sender 'team=AI Shipping Labs <team@aishippinglabs.com>'
run_app python manage.py shell <<'PY'
import os
from mailing.models import Client

client = Client.objects.get(organization__slug="aisl", slug="aisl-website")
secret = os.environ["RELAY_AISL_WEBHOOK_SECRET"].strip()
if not secret:
    raise ValueError("AISL webhook secret must be non-empty")
if client.relay_webhook_signing_secret and client.relay_webhook_signing_secret != secret:
    raise ValueError("Existing AISL webhook secret differs; coordinate rotation first")
client.relay_webhook_signing_secret = secret
origins = set(client.relay_webhook_allowed_origins)
origins.add("https://aishippinglabs.com")
client.relay_webhook_allowed_origins = sorted(origins)
client.save(update_fields=["relay_webhook_signing_secret", "relay_webhook_allowed_origins", "updated_at"])
print("AISL job webhook origin and signing secret configured.")
PY
echo 'AISL access provisioned. Credentials are in /etc/relay/aisl.env (mode 0600).'
