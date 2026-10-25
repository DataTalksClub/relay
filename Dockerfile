FROM python:3.13.5-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"
ENV UV_FROZEN=1

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends msmtp ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Release identity baked at build time so /health/ready can prove the NEW
# release is serving traffic. deploy-dev.yml passes these as build-args;
# prod promotes the dev-proven image, so the baked values travel with it.
# Defaults keep local dev/test working without build args.
ARG RELAY_IMAGE_TAG=unknown
ARG RELAY_SOURCE_SHA=unknown
ENV RELAY_IMAGE_TAG=${RELAY_IMAGE_TAG}
ENV RELAY_SOURCE_SHA=${RELAY_SOURCE_SHA}

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY . .
RUN uv run python manage.py collectstatic --noinput

EXPOSE 8000
ENTRYPOINT ["/bin/sh", "/app/scripts/container-entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-", "relay.wsgi:application"]
