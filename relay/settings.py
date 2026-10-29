import json
import os
import sys
import warnings
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def csv_env(name, default="", *, allow_empty=False):
    values = [value.strip() for value in os.environ.get(name, default).split(",") if value.strip()]
    if values or allow_empty:
        return values
    return [value.strip() for value in default.split(",") if value.strip()]


def bool_env(name, *, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes"}


def float_env(name, *, default):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return float(value)


def int_env(name, *, default):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return int(value)


DEBUG = bool_env("DEBUG", default=True)
TESTING = "test" in sys.argv or "pytest" in sys.argv[0]

DEV_FALLBACK_SECRET_KEY = "django-insecure-dev-only-do-not-use-in-production"
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
if not SECRET_KEY:
    if TESTING:
        SECRET_KEY = DEV_FALLBACK_SECRET_KEY
    elif DEBUG:
        warnings.warn("SECRET_KEY is not set; using a development-only fallback.", RuntimeWarning, stacklevel=2)
        SECRET_KEY = DEV_FALLBACK_SECRET_KEY
    else:
        raise ImproperlyConfigured("SECRET_KEY must be set when DEBUG=False.")

ALLOWED_HOSTS = csv_env("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")
CSRF_TRUSTED_ORIGINS = csv_env("CSRF_TRUSTED_ORIGINS", "", allow_empty=True)
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
AUTH_BASE_URL = os.environ.get("AUTH_BASE_URL", "" if DEBUG else "https://auth.dtcdev.click").rstrip("/")
AUTH_CLIENT_ID = os.environ.get("AUTH_CLIENT_ID", "" if DEBUG else "41gcnc18qjtq61rsag9iqoepmk")
AUTH_CALLBACK_URL = os.environ.get(
    "AUTH_CALLBACK_URL", "" if DEBUG else "https://relay.dtcdev.click/auth/callback"
)
AUTH_LOGOUT_URL = os.environ.get("AUTH_LOGOUT_URL", "" if DEBUG else "https://relay.dtcdev.click/")
AUTH_ISSUER = os.environ.get(
    "AUTH_ISSUER", "" if DEBUG else "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_H7nJu52Bs"
).rstrip("/")
AUTH_JWKS_URL = os.environ.get(
    "AUTH_JWKS_URL", f"{AUTH_ISSUER}/.well-known/jwks.json" if AUTH_ISSUER else ""
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_tasks",
    # Before django_tasks_db: relay's db_worker command overrides upstream's
    # with database self-healing (relay/management/commands/db_worker.py).
    "relay",
    "django_tasks_db",
    "taskdeck",
    "mailing",
    "jobs",
]

# Background tasks. Application code depends only on `django.tasks`; the
# backend below is a configuration choice, not an import anywhere in `mailing`.
TASKS = {
    "default": {
        "BACKEND": os.environ.get(
            "TASKS_BACKEND", "django_tasks_db.backend.DatabaseBackend"
        ),
        # "default" = campaign/transactional sends; "provider-events" = SES
        # webhook + inbound mail so bounce/complaint never wait behind a send
        # fan-out (F1 / t1126u). db_worker --queue-name selects the lane.
        "QUEUES": ["default", "provider-events"],
    }
}

# Transactional retries include the initial attempt; delayed tasks do not
# occupy a worker while waiting. Queue age uses created_at, never updated_at.
RELAY_TRANSACTIONAL_MAX_ATTEMPTS = max(1, int(os.environ.get("RELAY_TRANSACTIONAL_MAX_ATTEMPTS", "5")))
RELAY_TRANSACTIONAL_RETRY_BASE_SECONDS = max(1, int(os.environ.get("RELAY_TRANSACTIONAL_RETRY_BASE_SECONDS", "60")))
RELAY_TRANSACTIONAL_RETRY_MAX_SECONDS = max(1, int(os.environ.get("RELAY_TRANSACTIONAL_RETRY_MAX_SECONDS", "900")))
RELAY_TRANSACTIONAL_QUEUED_ALERT_MINUTES = max(1, int(os.environ.get("RELAY_TRANSACTIONAL_QUEUED_ALERT_MINUTES", "15")))
RELAY_TRANSACTIONAL_ALERT_REPEAT_SECONDS = max(1, int(os.environ.get("RELAY_TRANSACTIONAL_ALERT_REPEAT_SECONDS", "300")))
# Provider-events tasks (SES webhook, inbound mail) retry transient DB errors
# (deadlock, serialization failure, refused/closed connection, DNS miss):
# first in-process, then as delayed tasks with bounded exponential backoff
# (30s, 60s, ... capped at 15 min; ~30 min in total) so a transient error never
# leaves an event permanently FAILED. Attempts include the first one.
RELAY_PROVIDER_EVENT_DB_RETRY_ATTEMPTS = max(1, int(os.environ.get("RELAY_PROVIDER_EVENT_DB_RETRY_ATTEMPTS", "4")))
RELAY_PROVIDER_EVENT_MAX_ATTEMPTS = max(1, int(os.environ.get("RELAY_PROVIDER_EVENT_MAX_ATTEMPTS", "7")))
RELAY_PROVIDER_EVENT_RETRY_BASE_SECONDS = max(1, int(os.environ.get("RELAY_PROVIDER_EVENT_RETRY_BASE_SECONDS", "30")))
RELAY_PROVIDER_EVENT_RETRY_MAX_SECONDS = max(1, int(os.environ.get("RELAY_PROVIDER_EVENT_RETRY_MAX_SECONDS", "900")))
RELAY_TRANSACTIONAL_ALERT_WEBHOOK_URL = os.environ.get("RELAY_TRANSACTIONAL_ALERT_WEBHOOK_URL", "").strip()

# Identifies this project in the taskdeck status contract and in TaskRun rows.
TASKDECK_PROJECT = "relay"

# Bearer token the cross-project console presents to read the status
# contract. Empty disables the endpoint entirely (it 404s), so a host that
# has not been given a token cannot leak operational detail.
TASKDECK_STATUS_TOKEN = os.environ.get("TASKDECK_STATUS_TOKEN", "")

# Bearer token for GET /internal/transfer/export and POST /internal/transfer/load.
# Empty disables both routes (they 404). The document contains contacts and
# client secrets, so leave this unset except during a migration.
RELAY_TRANSFER_TOKEN = os.environ.get("RELAY_TRANSFER_TOKEN", "")

# Recurring sends that something outside this service triggers on a timer.
#
# Transactional reminders are scheduled by their caller, which decides who
# receives a reminder and when. Relay's campaign scheduler does not take over
# those caller-owned reminder schedules. What was missing is
# that a schedule which silently stops firing looked exactly like a quiet
# period. Declaring the expectation here lets the status contract report the
# last run against it, so a missed sweep is visible.
#
# `template_key` is how a run is recognised: the batch task records it in the
# run's message. `cron` and `max_age_s` are what the trigger is *expected* to
# do -- keep them in step with the EventBridge rules in
# aws-infra main/cmp/cmp_deadline_reminder.tf, which are the source of truth.
TASKDECK_SCHEDULES = [
    {
        "name": "deadline-reminders",
        "template_key": "deadline-reminder",
        "cron": "0 9 * * *",
        "max_age_s": 60 * 60 * 26,  # daily, with room for a late or slow run
    },
]

MIDDLEWARE = [
    "mailing.middleware.DatabaseOutageMiddleware",
    "mailing.middleware.DropContentLengthOnNoContent",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Local convenience only: with DEBUG on, every request is pre-authenticated
    # as the seeded superuser instead of hitting the admin login. Deployed
    # environments run with DEBUG off and authenticate through relay.oidc.
    "relay.dev_auth.DevAutoLoginMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "mailing.operator_scope.OperatorRecordScopeMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if TESTING:
    MIDDLEWARE.remove("whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "relay.urls"

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "relay-default",
        "TIMEOUT": 300,
        "OPTIONS": {"MAX_ENTRIES": 2000},
    }
}

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "mailing.context_processors.operator_client_context",
            ],
        },
    },
]

WSGI_APPLICATION = "relay.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
        conn_max_age=600,
        conn_health_checks=True,
    )
}

if DATABASES["default"]["ENGINE"].endswith("postgresql"):
    # Fail fast and detect dead peers instead of hanging on a stale socket.
    # On 2026-10-22/23 Coolify recreated Postgres with a new IP; long-lived
    # workers kept sockets to the old one. connect_timeout bounds a connect to
    # an address that no longer answers; TCP keepalives make the kernel notice
    # an idle connection whose peer vanished (30s idle + 3 x 10s probes), so the
    # next health check reconnects through fresh DNS. No IP is ever pinned.
    DATABASES["default"].setdefault("OPTIONS", {})
    DATABASES["default"]["OPTIONS"] = {
        "connect_timeout": int(os.environ.get("RELAY_DB_CONNECT_TIMEOUT_SECONDS", "5")),
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    } | DATABASES["default"]["OPTIONS"]

# Long-running loops touch this after every iteration that reached the
# database; worker_healthcheck fails when it goes stale (see mailing/db_retry.py).
RELAY_DB_HEARTBEAT_FILE = os.environ.get("RELAY_DB_HEARTBEAT_FILE", "/tmp/relay-db-heartbeat")

if DATABASES["default"]["ENGINE"].endswith("sqlite3"):
    # On a single host this database has many writers: two gunicorn workers,
    # the task worker, and five queue workers. SQLite's default is to fail
    # immediately when another connection holds the write lock, which surfaces
    # as "database is locked" on a bulk send rather than as a wait.
    #
    # WAL lets readers continue while one writer works, `timeout` makes a
    # blocked writer wait instead of raising, and IMMEDIATE takes the write
    # lock when a write transaction opens rather than upgrading mid-way --
    # the upgrade is what turns two concurrent transactions into an error
    # neither can retry.
    #
    # None of this makes SQLite a good fit for this many writers. It buys room
    # until the move to Postgres; see docs/managed-database-migration.md.
    DATABASES["default"].setdefault("OPTIONS", {})
    DATABASES["default"]["OPTIONS"] |= {
        "timeout": 30,
        "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
        "transaction_mode": "IMMEDIATE",
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Where the stock auth views send an already-authenticated visitor (e.g. after
# logging out locally, the admin login page bounces here). The OIDC flow keeps
# its own explicit return_to handling and never reads this.
LOGIN_REDIRECT_URL = "/"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
if TESTING:
    STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "newsletter@example.com")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_SES_REGION = os.environ.get("AWS_SES_REGION", AWS_REGION)
AWS_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL", "")
DMARC_READER_ROLE_ARN = os.environ.get("DMARC_READER_ROLE_ARN", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
API_DOCS_BASE_URL = os.environ.get("RELAY_API_DOCS_BASE_URL", PUBLIC_BASE_URL).rstrip("/")
# Public base URL of the client site page that receives double opt-in confirm
# links. Relay appends `?token=...`; the token never carries a raw email.
SUBSCRIPTION_CONFIRM_BASE_URL = (
    os.environ.get("SUBSCRIPTION_CONFIRM_BASE_URL", "").strip().rstrip("/") or PUBLIC_BASE_URL
)
# Where "Block this sender" links in forwarded mail point. Empty means the
# console host from AUTH_CALLBACK_URL, which serves every Relay path.
INBOUND_BLOCK_LINK_BASE_URL = os.environ.get("INBOUND_BLOCK_LINK_BASE_URL", "").strip().rstrip("/")
# Static sites call these lists without a client API key. The value is parsed
# by mailing.services.public_lists. Empty disables the public routes' lists.
RELAY_PUBLIC_LISTS = os.environ.get("RELAY_PUBLIC_LISTS", "")
RELAY_PUBLIC_SUBSCRIBE_ORIGINS = frozenset(csv_env("RELAY_PUBLIC_SUBSCRIBE_ORIGINS", "", allow_empty=True))
RELAY_PUBLIC_SUBSCRIBE_EMAIL_LIMIT = int(os.environ.get("RELAY_PUBLIC_SUBSCRIBE_EMAIL_LIMIT", "3"))
RELAY_PUBLIC_SUBSCRIBE_EMAIL_WINDOW_SECONDS = int(
    os.environ.get("RELAY_PUBLIC_SUBSCRIBE_EMAIL_WINDOW_SECONDS", "3600")
)
AWS_SES_CONFIGURATION_SET = os.environ.get("AWS_SES_CONFIGURATION_SET", "")
# domain role=<arn> region=<region> configuration_set=<name>, separated by ';'
# or newlines. Domains absent from this list use email.send and the values above.
RELAY_SES_DOMAIN_ROUTES = os.environ.get("RELAY_SES_DOMAIN_ROUTES", "")
SES_MAX_SEND_RATE_PER_SECOND = float_env("RELAY_SES_MAX_SEND_RATE", default=10.0)
SQS_TRANSACTIONAL_EMAIL_QUEUE_URL = os.environ.get("SQS_TRANSACTIONAL_EMAIL_QUEUE_URL", "")
SQS_CAMPAIGN_EMAIL_QUEUE_URL = os.environ.get("SQS_CAMPAIGN_EMAIL_QUEUE_URL", "")
SQS_EMAIL_EVENTS_QUEUE_URL = os.environ.get("SQS_EMAIL_EVENTS_QUEUE_URL", "")
SQS_SES_WEBHOOKS_QUEUE_URL = os.environ.get("SQS_SES_WEBHOOKS_QUEUE_URL", "")
SQS_AISL_SES_WEBHOOKS_QUEUE_URL = os.environ.get("SQS_AISL_SES_WEBHOOKS_QUEUE_URL", "")
AISL_SES_FORWARD_ENABLED = bool_env("AISL_SES_FORWARD_ENABLED", default=False)
AISL_SES_WEBHOOK_URL = os.environ.get("AISL_SES_WEBHOOK_URL", "https://aishippinglabs.com/api/ses-events")
AISL_SES_WEBHOOK_SECRET_ARN = os.environ.get("AISL_SES_WEBHOOK_SECRET_ARN", "")
AISL_SES_TOPIC_ARNS = tuple(filter(None, (
    value.strip() for value in os.environ.get("AISL_SES_TOPIC_ARNS", "").split(",")
)))

# Only transport IDs and acknowledgement status are logged by this adapter.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
        "aisl_ses_console": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "mailing": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": True,
        },
        "mailing.services.aisl_ses_forwarder": {
            "handlers": ["aisl_ses_console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
SQS_INBOUND_EMAIL_QUEUE_URL = os.environ.get("SQS_INBOUND_EMAIL_QUEUE_URL", "")
SQS_SANDBOX_SES_WEBHOOKS_QUEUE_URL = os.environ.get(
    "SQS_SANDBOX_SES_WEBHOOKS_QUEUE_URL",
    "https://sqs.eu-west-1.amazonaws.com/817685572750/relay-sandbox-ses-webhooks",
)
SQS_SANDBOX_INBOUND_EMAIL_QUEUE_URL = os.environ.get("SQS_SANDBOX_INBOUND_EMAIL_QUEUE_URL", "")
INBOUND_EMAIL_EVENTS_TOPIC_ARN = os.environ.get("INBOUND_EMAIL_EVENTS_TOPIC_ARN", "")
# INBOUND_EMAIL_IDEMPOTENCY_TABLE is deliberately not read any more. Idempotency
# is a unique constraint on inbound_messages.message_id, inside the transaction
# that was already happening, so it needs no table and cannot half-succeed. The
# env var is left set in deployed environments until nobody reads it.
INBOUND_EMAIL_ARTIFACT_PREFIX = os.environ.get("INBOUND_EMAIL_ARTIFACT_PREFIX", "processed/")
INBOUND_EMAIL_ROUTES = {
    address.strip().lower(): route.strip()
    for address, separator, route in (
        item.partition("=") for item in os.environ.get("INBOUND_EMAIL_ROUTES", "").split(",") if item.strip()
    )
    if separator and address.strip() and route.strip()
}
WORKER_STATUS_SYSTEMD_ENABLED = bool_env("RELAY_WORKER_STATUS_SYSTEMD_ENABLED", default=not TESTING)
WORKER_STATUS_SYSTEMD_TIMEOUT_SECONDS = float_env("RELAY_WORKER_STATUS_SYSTEMD_TIMEOUT_SECONDS", default=1.5)
RELAY_WEBHOOK_TIMEOUT_SECONDS = float_env("RELAY_WEBHOOK_TIMEOUT_SECONDS", default=30.0)
RELAY_WEBHOOK_MAX_TIMEOUT_SECONDS = float_env("RELAY_WEBHOOK_MAX_TIMEOUT_SECONDS", default=60.0)
RELAY_JOB_MAX_ATTEMPTS = int(os.environ.get("RELAY_JOB_MAX_ATTEMPTS", "5"))
RELAY_JOB_RETRY_BASE_SECONDS = float_env("RELAY_JOB_RETRY_BASE_SECONDS", default=5.0)
RELAY_WEBHOOK_MAX_LEASE_SECONDS = float_env("RELAY_WEBHOOK_MAX_LEASE_SECONDS", default=3600.0)
RELAY_WEBHOOK_MAX_INFLIGHT_PER_CLIENT = int(os.environ.get("RELAY_WEBHOOK_MAX_INFLIGHT_PER_CLIENT", "4"))
RELAY_WEBHOOK_RATE_LIMIT_PER_MINUTE = int(os.environ.get("RELAY_WEBHOOK_RATE_LIMIT_PER_MINUTE", "60"))
try:
    RELAY_TASK_ROLE_ARNS = json.loads(os.environ.get("RELAY_TASK_ROLE_ARNS", "{}"))
except json.JSONDecodeError as exc:
    raise ImproperlyConfigured("RELAY_TASK_ROLE_ARNS must be a JSON object.") from exc
if not isinstance(RELAY_TASK_ROLE_ARNS, dict):
    raise ImproperlyConfigured("RELAY_TASK_ROLE_ARNS must be a JSON object.")
RELAY_REQUIRE_TASK_ROLES = bool_env("RELAY_REQUIRE_TASK_ROLES", default=False)
TRANSACTIONAL_EMAIL_QUEUE_NAME = os.environ.get("TRANSACTIONAL_EMAIL_QUEUE_NAME", "transactional-email")
CAMPAIGN_EMAIL_QUEUE_NAME = os.environ.get("CAMPAIGN_EMAIL_QUEUE_NAME", "campaign-email")
SES_WEBHOOKS_QUEUE_NAME = os.environ.get("SES_WEBHOOKS_QUEUE_NAME", "ses-webhooks")
EMAIL_EVENTS_QUEUE_NAME = os.environ.get("EMAIL_EVENTS_QUEUE_NAME", "email-events")
CMP_WEBHOOK_URL = os.environ.get("CMP_WEBHOOK_URL", "").strip()
CMP_WEBHOOK_TOKEN = os.environ.get("CMP_WEBHOOK_TOKEN", "")
CMP_WEBHOOK_TIMEOUT_SECONDS = float_env("CMP_WEBHOOK_TIMEOUT_SECONDS", default=3.0)
CLIENT_CALLBACK_TIMEOUT_SECONDS = float_env("CLIENT_CALLBACK_TIMEOUT_SECONDS", default=3.0)
CLIENT_CALLBACK_MAX_ATTEMPTS = int(float_env("CLIENT_CALLBACK_MAX_ATTEMPTS", default=8))
CLIENT_CALLBACK_RETRY_BASE_SECONDS = float_env("CLIENT_CALLBACK_RETRY_BASE_SECONDS", default=60.0)
CLIENT_CALLBACK_RETRY_MAX_DELAY_SECONDS = float_env("CLIENT_CALLBACK_RETRY_MAX_DELAY_SECONDS", default=21600.0)
MAILCHIMP_TIMEOUT_SECONDS = float_env("MAILCHIMP_TIMEOUT_SECONDS", default=5.0)
SES_WEBHOOKS_SIGNATURE_MODE = os.environ.get(
    "SES_WEBHOOKS_SIGNATURE_MODE",
    "mock" if DEBUG or TESTING else "strict",
)
SES_WEBHOOKS_ALLOW_SUBSCRIPTION_CONFIRMATION = bool_env("SES_WEBHOOKS_ALLOW_SUBSCRIPTION_CONFIRMATION", default=False)
# F4: harden the public HTTP fallback. Bodies over this size are rejected
# before Django buffers them or any SNS signature work (cert fetch + verify),
# with HTTP 413. SNS always sends Content-Length; missing values are rejected.
SES_WEBHOOKS_MAX_BODY_BYTES = int_env("SES_WEBHOOKS_MAX_BODY_BYTES", default=256 * 1024)
# F4: optional TopicArn allowlist for the HTTP fallback. Empty allows any
# topic once the SNS signature verifies (backwards compatible); set this in
# production to the SES configuration-set topic ARN(s) so events from other
# AWS accounts' topics are rejected with 403.
SES_WEBHOOKS_ALLOWED_TOPIC_ARNS = csv_env("SES_WEBHOOKS_ALLOWED_TOPIC_ARNS", "", allow_empty=True)
SES_WEBHOOKS_CERT_CACHE_TTL_SECONDS = float_env("SES_WEBHOOKS_CERT_CACHE_TTL_SECONDS", default=3600.0)

# Transactional template test sends go only to these addresses (R1.3).
# Empty disables test-send entirely.
TRANSACTIONAL_TEST_SEND_ALLOWLIST = csv_env("TRANSACTIONAL_TEST_SEND_ALLOWLIST", "", allow_empty=True)

# Site host treated as internal by the markdown external-link rewriter
# (ported from the AISL email pipeline). Branding lives in per-client
# EmailLayout rows; there is no global brand name and no Relay fallback.
RELAY_EMAIL_SITE_BASE_URL = os.environ.get("RELAY_EMAIL_SITE_BASE_URL", "")

# No-history clients never page. Overrides keyed by client ID, without secrets.
RELAY_TRANSACTIONAL_MONITOR_ENABLED = os.environ.get("RELAY_TRANSACTIONAL_MONITOR_ENABLED", "1") == "1"
RELAY_TRANSACTIONAL_MONITOR_CLIENTS = json.loads(os.environ.get("RELAY_TRANSACTIONAL_MONITOR_CLIENTS", "{}"))
RELAY_TRANSACTIONAL_MONITOR_FROM = os.environ.get("RELAY_TRANSACTIONAL_MONITOR_FROM", "noreply@datatalks.club")
RELAY_TRANSACTIONAL_MONITOR_TO = os.environ.get("RELAY_TRANSACTIONAL_MONITOR_TO", "dev@dtcdev.click")
MIDDLEWARE.insert(0, "mailing.transactional_monitor_middleware.TransactionalMonitorMiddleware")

RELAY_TRANSACTIONAL_MONITOR_MSMTP_CONFIG = os.environ.get("RELAY_TRANSACTIONAL_MONITOR_MSMTP_CONFIG", "")

RELAY_SES_MONITOR_ENABLED = os.environ.get("RELAY_SES_MONITOR_ENABLED", "1") == "1"
RELAY_SES_OUTCOME_GAP_THRESHOLD = int(os.environ.get("RELAY_SES_OUTCOME_GAP_THRESHOLD", "1"))
RELAY_SES_OUTCOME_GAP_LOOKBACK_DAYS = int(os.environ.get("RELAY_SES_OUTCOME_GAP_LOOKBACK_DAYS", "7"))
RELAY_SES_EVENTS_TOPIC_NAME = os.environ.get("RELAY_SES_EVENTS_TOPIC_NAME", "relay-production-ses-events")
