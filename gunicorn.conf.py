"""Shared by Docker, Coolify and the sandbox Gunicorn command."""

from relay.gunicorn_logging import ACCESS_LOG_FORMAT

access_log_format = ACCESS_LOG_FORMAT
logger_class = "relay.gunicorn_logging.PrivateAccessLogger"
