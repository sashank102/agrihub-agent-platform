# syntax=docker/dockerfile:1

FROM python:3.12.11-slim-bookworm AS build

COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY tests ./tests
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.11-slim-bookworm AS runtime

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src
COPY docker/api-entrypoint.sh /entrypoint.sh
RUN chmod 0555 /entrypoint.sh \
    && chown -R app:app /app
USER app
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WEB_CONCURRENCY=1 \
    API_HOST=0.0.0.0 \
    API_PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=40s --retries=10 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
ENTRYPOINT ["/entrypoint.sh"]
