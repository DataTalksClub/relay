FROM python:3.13.5-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/

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
RUN groupadd --system --gid 10001 relay \
    && useradd --system --uid 10001 --gid relay --home-dir /home/relay --create-home relay \
    && chown -R relay:relay /app

ENV HOME=/home/relay
USER relay

# Collect static assets at build time without making the deployed default DEBUG.
RUN DEBUG=1 uv run python manage.py collectstatic --noinput

EXPOSE 8000
ENTRYPOINT ["/bin/sh", "/app/scripts/container-entrypoint.sh"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "--bind", "0.0.0.0:8000", "--workers", "3", "--worker-class", "gthread", "--threads", "8", "--timeout", "60", "--access-logfile", "-", "relay.wsgi:application"]
