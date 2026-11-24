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
  ghcr.io/astral-sh/uv:0.13.8-python3.14-trixie-slim@sha256:81a1340fea1e72ad4a8e9a016128b0c61041cf182ea49fd788023e52842bc147 \
  "$@"
