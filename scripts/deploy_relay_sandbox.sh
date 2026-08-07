#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <git-commit>" >&2
  exit 2
fi

readonly release="$1"
readonly app_dir="/opt/relay"
readonly runtime_env="/etc/relay/runtime.env"
readonly infra_env="/etc/relay/infrastructure.env"
readonly image="relay:${release}"
readonly network="relay"

if [[ ! -f "$infra_env" ]]; then
  echo "missing $infra_env; Terraform bootstrap has not completed" >&2
  exit 1
fi

install -d -m 0755 /etc/relay /var/lib/relay/postgres /var/lib/relay/caddy-data /var/lib/relay/caddy-config

if [[ ! -f "$runtime_env" ]]; then
  umask 077
  secret_key="$(openssl rand -hex 48)"
  postgres_password="$(openssl rand -hex 32)"
  api_public_id="dtccourses$(openssl rand -hex 4)"
  api_secret="$(openssl rand -hex 32)"
  status_token="$(openssl rand -hex 32)"
  cat >"$runtime_env" <<ENV
DEBUG=False
SECRET_KEY=${secret_key}
ALLOWED_HOSTS=relay.dtcdev.click,127.0.0.1,localhost
CSRF_TRUSTED_ORIGINS=https://relay.dtcdev.click
DATABASE_URL=postgresql://relay:${postgres_password}@127.0.0.1:5432/relay
POSTGRES_DB=relay
POSTGRES_USER=relay
POSTGRES_PASSWORD=${postgres_password}
DEFAULT_FROM_EMAIL=DataTalks.Club Courses <courses@relay.dtcdev.click>
RELAY_API_DOCS_BASE_URL=https://relay.dtcdev.click
RELAY_SES_MAX_SEND_RATE=1
RELAY_BOOTSTRAP_API_KEY=relay_${api_public_id}_${api_secret}
TASKDECK_STATUS_TOKEN=${status_token}
RELAY_REQUIRE_TASK_ROLES=True
RELAY_WORKER_STATUS_SYSTEMD_ENABLED=False
ENV
  chmod 0600 "$runtime_env"
fi

grep -q '^RELAY_EMAIL_SEND_ROLE_ARN=' "$infra_env" || {
  echo "RELAY_EMAIL_SEND_ROLE_ARN is missing from $infra_env" >&2
  exit 1
}

email_role_arn="$(sed -n 's/^RELAY_EMAIL_SEND_ROLE_ARN=//p' "$infra_env")"
if ! grep -q '^RELAY_TASK_ROLE_ARNS=' "$runtime_env"; then
  printf 'RELAY_TASK_ROLE_ARNS={"email.send":"%s"}\n' "$email_role_arn" >>"$runtime_env"
fi

if [[ ! -d "$app_dir/.git" ]]; then
  if [[ -f "$app_dir/README" ]]; then
    mv "$app_dir/README" /var/lib/relay/bootstrap-readme
  fi
  git -C "$app_dir" init
  git -C "$app_dir" remote add origin https://github.com/DataTalksClub/relay.git
fi
git -C "$app_dir" fetch --prune origin main
git -C "$app_dir" checkout --force "$release"
git -C "$app_dir" clean -ffd -e .env

docker network inspect "$network" >/dev/null 2>&1 || docker network create "$network"
docker build --pull --tag "$image" "$app_dir"
docker pull postgres:17-alpine
docker pull caddy:2-alpine

log_options=(
  --log-driver awslogs
  --log-opt awslogs-region=eu-west-1
  --log-opt awslogs-group=/relay/sandbox/host
)

replace_container() {
  local name="$1"
  shift
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run --detach --name "$name" --restart unless-stopped \
    "${log_options[@]}" --log-opt "awslogs-stream=${name}" "$@"
}

replace_container relay-postgres \
  --env-file "$runtime_env" \
  --publish 127.0.0.1:5432:5432 \
  --volume /var/lib/relay/postgres:/var/lib/postgresql/data \
  postgres:17-alpine

for _ in $(seq 1 60); do
  if docker exec relay-postgres pg_isready -U relay -d relay >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
docker exec relay-postgres pg_isready -U relay -d relay

run_app() {
  docker run --rm --network host \
    --env-file "$infra_env" \
    --env-file "$runtime_env" \
    "$image" "$@"
}

run_app python manage.py migrate --noinput
run_app python manage.py collectstatic --noinput
run_app python manage.py provision_client_scope \
  --organization datatalksclub \
  --organization-name DataTalksClub \
  --audience dtc-courses \
  --audience-name "DataTalksClub Courses" \
  --client dtc-courses \
  --client-name "DTC Courses"
run_app python manage.py provision_client_api_key \
  --organization datatalksclub \
  --client dtc-courses \
  --name sandbox-deployment
run_app python manage.py set_client_senders dtc-courses \
  --organization datatalksclub \
  --default-sender courses \
  --sender 'courses=DataTalks.Club Courses <courses@relay.dtcdev.click>' \
  --sender 'no-reply=DataTalksClub <no-reply@relay.dtcdev.click>'

app_container_args=(
  --network host
  --env-file "$infra_env"
  --env-file "$runtime_env"
  "$image"
)

replace_container relay-web "${app_container_args[@]}" \
  gunicorn --bind 127.0.0.1:8000 --workers 2 --timeout 60 --access-logfile - relay.wsgi:application
replace_container relay-worker "${app_container_args[@]}" \
  python manage.py db_worker --interval 1
replace_container relay-scheduler "${app_container_args[@]}" \
  python manage.py run_relay_scheduler --interval 15
replace_container relay-ses-ingress "${app_container_args[@]}" \
  python manage.py drain_sqs_ingress ses-webhooks --batch-size 10 --wait-time 20
replace_container relay-inbound-ingress "${app_container_args[@]}" \
  python manage.py drain_sqs_ingress inbound-email --batch-size 10 --wait-time 20
replace_container relay-cmp-callbacks "${app_container_args[@]}" \
  python manage.py process_cmp_callbacks --batch-size 25 --idle-sleep 5
replace_container relay-recipient-imports "${app_container_args[@]}" \
  python manage.py process_recipient_list_imports --batch-size 10 --idle-sleep 5

replace_container relay-caddy \
  --network host \
  --volume "$app_dir/deploy/Caddyfile:/etc/caddy/Caddyfile:ro" \
  --volume /var/lib/relay/caddy-data:/data \
  --volume /var/lib/relay/caddy-config:/config \
  caddy:2-alpine run --config /etc/caddy/Caddyfile --adapter caddyfile

required_containers=(
  relay-postgres
  relay-web
  relay-worker
  relay-scheduler
  relay-ses-ingress
  relay-inbound-ingress
  relay-cmp-callbacks
  relay-recipient-imports
  relay-caddy
)

for _ in $(seq 1 60); do
  if curl --fail --silent http://127.0.0.1:8000/health/ready >/dev/null; then
    break
  fi
  sleep 2
done
curl --fail --show-error --silent http://127.0.0.1:8000/health/ready

for container in "${required_containers[@]}"; do
  [[ "$(docker inspect --format '{{.State.Running}}' "$container")" == "true" ]]
done

run_app python scripts/smoke_test_relay.py --base-url http://127.0.0.1:8000
run_app python scripts/upsert_cmp_templates.py \
  --base-url http://127.0.0.1:8000

curl --fail --show-error --silent --retry 20 --retry-delay 3 https://relay.dtcdev.click/health/ready
echo "Relay sandbox deployed: ${release}"
