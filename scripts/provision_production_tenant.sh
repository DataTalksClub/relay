#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 [--dry-run] [--help]" >&2
}

help() {
  cat <<HELP
Usage: $0 [--dry-run] [--help]

Provision the Relay production tenant for the DataTalks.Club website on this
host:

  - organisation datatalksclub
  - audience dtc
  - client dtc-website
  - configured senders: hello=DataTalks.Club <hello@datatalks.club> (default)
  - one client API key named production-deployment

The API key material is generated on this host into the 0600 runtime env file
(/etc/relay/runtime.env), following the sandbox RELAY_BOOTSTRAP_API_KEY
mechanism. The script never prints or embeds key material; the command output
shows only the key name and its public prefix. Copy the key from
/etc/relay/runtime.env on this host into the DTC website deployment secrets.

Run this on the production host (for example through SSM Session Manager)
after the first production deploy has built the release image. It is
idempotent: organisation, audience, client, and key rows are updated in place,
and the key is regenerated only if absent from the runtime env file.

Options:
  --dry-run   Print the actions that would run, without changing anything.
  --help      Show this help.
HELP
}

dry_run=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      dry_run=true
      shift
      ;;
    --help)
      help
      exit 0
      ;;
    -*)
      usage
      exit 2
      ;;
    --)
      shift
      break
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

readonly app_dir="/opt/relay"
readonly runtime_env="/etc/relay/runtime.env"
readonly infra_env="/etc/relay/infrastructure.env"
readonly organization=datatalksclub
readonly organization_name="DataTalks.Club"
readonly audience=dtc
readonly audience_name="DataTalks.Club"
readonly client=dtc-website
readonly client_name="DataTalks.Club Website"
readonly api_key_name=production-deployment
readonly default_sender_id=hello
readonly default_sender='hello=DataTalks.Club <hello@datatalks.club>'

describe_plan() {
  echo "Provisioning plan for Relay production tenant:"
  echo "  organization: ${organization} (${organization_name})"
  echo "  audience: ${audience} (${audience_name})"
  echo "  client: ${client} (${client_name})"
  echo "  api key name: ${api_key_name} (generated on the host if absent; value never displayed)"
  echo "  senders: ${default_sender}"
  echo "Commands that would run on this host:"
  echo "  docker run --rm --network host --env-file ${infra_env} --env-file ${runtime_env} <release image> \\"
  echo "    python manage.py provision_client_scope --organization ${organization} --organization-name '${organization_name}' \\"
  echo "      --audience ${audience} --audience-name '${audience_name}' --client ${client} --client-name '${client_name}'"
  echo "  ensure RELAY_BOOTSTRAP_API_KEY in ${runtime_env} (mode 0600, generated if absent, never printed)"
  echo "  docker run ... python manage.py provision_client_api_key --organization ${organization} --client ${client} --name ${api_key_name}"
  echo "  docker run ... python manage.py set_client_senders ${client} --organization ${organization} \\"
  echo "    --default-sender ${default_sender_id} --sender '${default_sender}'"
}

if [[ "$dry_run" == true ]]; then
  describe_plan
  exit 0
fi

if [[ ! -f "$infra_env" ]]; then
  echo "missing $infra_env; Terraform bootstrap has not completed" >&2
  exit 1
fi
if [[ ! -f "$runtime_env" ]]; then
  echo "missing $runtime_env; run the production deploy once first so the runtime env exists" >&2
  exit 1
fi

release="$(git -C "$app_dir" rev-parse HEAD)"
image="relay:${release}"
if ! docker image inspect "$image" >/dev/null 2>&1; then
  echo "missing image ${image}; run the production deploy for this release first" >&2
  exit 1
fi

run_app() {
  docker run --rm --network host \
    --env-file "$infra_env" \
    --env-file "$runtime_env" \
    "$image" "$@"
}

if ! grep -q '^RELAY_BOOTSTRAP_API_KEY=' "$runtime_env"; then
  api_public_id="dtcwebsite$(openssl rand -hex 4)"
  api_secret="$(openssl rand -hex 32)"
  umask 077
  printf 'RELAY_BOOTSTRAP_API_KEY=relay_%s_%s\n' "$api_public_id" "$api_secret" >>"$runtime_env"
  chmod 0600 "$runtime_env"
  echo "Generated a new client API key into ${runtime_env} (mode 0600); the value is not displayed."
fi

run_app python manage.py provision_client_scope \
  --organization "$organization" \
  --organization-name "$organization_name" \
  --audience "$audience" \
  --audience-name "$audience_name" \
  --client "$client" \
  --client-name "$client_name"

run_app python manage.py provision_client_api_key \
  --organization "$organization" \
  --client "$client" \
  --name "$api_key_name"

run_app python manage.py set_client_senders "$client" \
  --organization "$organization" \
  --default-sender "$default_sender_id" \
  --sender "$default_sender"

echo "Production tenant provisioned: organization=${organization} audience=${audience} client=${client}"
echo "Copy RELAY_BOOTSTRAP_API_KEY from ${runtime_env} into the DTC website deployment secrets."
