# Container image for running coros-mcp over HTTP (e.g. Google Cloud Run).
# See docs/cloud-run.md. Local stdio use does not need this.

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra cloud --no-install-project
COPY coros_mcp ./coros_mcp
RUN uv sync --frozen --no-dev --extra cloud --no-editable

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    COROS_CACHE_DB=/tmp/cache.db
USER app
WORKDIR /home/app
EXPOSE 8080
CMD ["coros-mcp", "serve-http"]
