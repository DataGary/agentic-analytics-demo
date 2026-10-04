# One image, two entrypoints (agent, mcp-server) plus the one-shot seed job.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PYTHONPATH=/app/src

WORKDIR /app
# Dependencies first so code edits don't bust the dependency layer cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

# Run as non-root. Chainlit writes .chainlit/ and .files/ in the working dir, so /app must be writable.
RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app
