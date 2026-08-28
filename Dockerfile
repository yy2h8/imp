# Build stage: resolve dependencies into a virtualenv with uv.
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /uvx /bin/
ENV UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Runtime stage: plain Python image, virtualenv + source only.
FROM python:3.13-slim
RUN useradd --create-home --uid 1000 agent
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY imp /app/imp
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app" \
    PYTHONUNBUFFERED=1
WORKDIR /workspace
USER agent
ENTRYPOINT ["python", "-m", "imp.cli"]
