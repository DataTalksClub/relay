FROM python:3.13.5-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"
ENV UV_FROZEN=1

WORKDIR /app

# git is needed at build time only: taskdeck is pinned to a git revision rather
# than published to an index. Removed in the same layer so it does not ship.
COPY pyproject.toml uv.lock ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && uv sync --frozen \
    && apt-get purge -y git \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN uv run python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["uv", "run", "python", "manage.py", "runserver", "0.0.0.0:8000"]
