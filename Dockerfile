# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY followthrough ./followthrough
RUN uv sync --locked --no-dev
RUN uv run --module livekit.agents download-files

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
RUN adduser --disabled-password --gecos "" --home /app --uid 10001 appuser
COPY --from=build --chown=appuser:appuser /app /app
WORKDIR /app
USER appuser

CMD ["python", "-m", "followthrough.livekit_agent", "start"]
