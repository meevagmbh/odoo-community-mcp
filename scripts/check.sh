#!/bin/sh
# Same checks as CI (format, lint, types, unit tests). CI only checks, never rewrites.
# Uses a local uv when RUN is unset and uv >= 0.13.8 is available, else the dev container.
set -eu
cd "$(dirname "$0")/.."
if [ -z "${RUN:-}" ]; then
  if command -v uv >/dev/null 2>&1 && uv run --no-sync python -c 'import sys; sys.exit(sys.version_info < (3, 15))' >/dev/null 2>&1; then
    RUN="uv run"
  else
    RUN="scripts/dev.sh uv run"
  fi
fi
case "${1:-all}" in
  quick) $RUN sh -c 'ruff format --check . && ruff check .' ;;
  all) $RUN sh -c 'ruff format --check . && ruff check . && mypy && pytest -q --cov' ;;
  *) echo "usage: $0 [quick|all]" >&2; exit 2 ;;
esac
