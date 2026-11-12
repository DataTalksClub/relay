"""Shared by Docker, Coolify and the sandbox Gunicorn command.

Sizing: at least 2 gthread workers with threads so one slow request cannot
block health checks or other users. Tuned via env with safe defaults:

  GUNICORN_WORKERS (default 3), GUNICORN_THREADS (default 8),
  GUNICORN_TIMEOUT (default 60), GUNICORN_BIND (default 0.0.0.0:8000).

Gunicorn auto-loads this file from the image WORKDIR (/app), so the Coolify
web command is intentionally left untouched: the dev/prod deploy workflows
refuse to overwrite the live web service when it differs from the template,
and CLI flags override this file. Effective Coolify runtime is therefore
CLI `--workers 2` + this file's gthread/threads/timeout (verified with
`gunicorn --print-config`: workers=2, gthread, threads=8). Dockerfile and the
sandbox deploy set their own explicit flags.
"""

import os

from relay.gunicorn_logging import ACCESS_LOG_FORMAT

# Web processes only: bound row-lock and statement waits. Workers never load
# this file, so long batch transactions keep their normal behaviour. The
# shorter lock wait lets requests fail before they pin every web thread.
os.environ.setdefault("RELAY_WEB_DB_LOCK_TIMEOUT_MS", "2000")
os.environ.setdefault("RELAY_WEB_DB_STATEMENT_TIMEOUT_MS", "15000")

access_log_format = ACCESS_LOG_FORMAT
logger_class = "relay.gunicorn_logging.PrivateAccessLogger"


def _int_env(name, default, minimum=1):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")
workers = _int_env("GUNICORN_WORKERS", 3, minimum=2)
worker_class = "gthread"
threads = _int_env("GUNICORN_THREADS", 8, minimum=2)
timeout = _int_env("GUNICORN_TIMEOUT", 60)
graceful_timeout = _int_env("GUNICORN_GRACEFUL_TIMEOUT", 30)
keepalive = _int_env("GUNICORN_KEEPALIVE", 5)
