# Build stage: resolve dependencies into a virtualenv with uv.
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /uvx /bin/
ENV UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Shared runtime base: plain Python image, virtualenv + imp source.
FROM python:3.13-slim AS base
RUN useradd --create-home --uid 1000 agent
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY imp /app/imp
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app" \
    PYTHONUNBUFFERED=1

# Assistant service image: docker build --target assistant (see assistant/README.md).
FROM base AS assistant
COPY assistant /app/assistant
RUN mkdir /data && chown agent:agent /data
WORKDIR /data
USER agent
ENTRYPOINT ["python", "-m", "assistant"]

# CLI image: kept last so a plain `docker build -t imp .` is unchanged.
FROM base
WORKDIR /workspace
USER agent
ENTRYPOINT ["python", "-m", "imp.cli"]
