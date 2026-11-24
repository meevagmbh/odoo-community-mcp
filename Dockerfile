# syntax=docker/dockerfile:1.12
#
# Minimal, rootless runtime image:
# - Chainguard Python base: no shell, no package manager, few CVEs, nonroot (65532)
# - only the locked runtime dependencies in a venv, no build tools
# - works with a read-only root filesystem and all capabilities dropped
# Build and runtime bases must ship the same Python minor; Dependabot updates them together.

FROM ghcr.io/astral-sh/uv:0.13.10@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f AS uv

# --- build: resolve the locked dependencies into a self-contained venv -------
FROM cgr.dev/chainguard/python:latest-dev@sha256:3ca53097911a867be4f8190b8bc272d1465e9d95ef32ecc2cd70d841e2baeac3 AS build

USER root
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/bin/python3 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable \
 && /app/.venv/bin/python -c "import odoo_mcp.app"

# --- runtime ------------------------------------------------------------------
FROM cgr.dev/chainguard/python:latest@sha256:3a89a9027f47e44e9f9da201493013249a02fc87fa7269bba80f621d7ade83f8

LABEL org.opencontainers.image.title="odoo-community-mcp" \
      org.opencontainers.image.description="Stateless MCP gateway for Odoo 19+ (per-user API key pass-through)" \
      org.opencontainers.image.source="https://github.com/meevagmbh/odoo-community-mcp" \
      org.opencontainers.image.licenses="MIT"

# Root-owned and therefore read-only for the runtime user.
COPY --from=build --chown=0:0 /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

USER 65532:65532
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('MCP_PORT', '8000') + '/healthz', timeout=2)"]

ENTRYPOINT ["/app/.venv/bin/odoo-community-mcp"]
