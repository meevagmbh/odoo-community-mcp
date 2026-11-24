#!/bin/sh
# Run a command inside the pinned uv/Python image (no local Python 3.14 needed).
# Usage: scripts/dev.sh uv run pytest
set -eu
cache="${XDG_CACHE_HOME:-$HOME/.cache}/odoo-community-mcp-dev"
mkdir -p "$cache"
exec docker run --rm \
  -v "$(pwd)":/app -w /app \
  -v "$cache":/cache \
  -u "$(id -u):$(id -g)" \
  -e HOME=/cache/home \
  -e UV_CACHE_DIR=/cache/uv \
  -e UV_PROJECT_ENVIRONMENT=/cache/venv \
  -e UV_PYTHON_DOWNLOADS=never \
  ghcr.io/astral-sh/uv:0.13.8-python3.15-trixie-slim@sha256:053efa9652f89ac056731b55b4912131fca1dd644d472b6a40340a62f183a5db \
  "$@"
