#!/bin/sh
# Start a throwaway Odoo 20, seed two users with API keys, run the integration tests.
# Usage: scripts/integration.sh            (tests run via scripts/dev.sh)
#        RUNNER="uv run" scripts/integration.sh   (tests run with a local uv, e.g. in CI)
#        ODOO_IMAGE=odoo:19.0 scripts/integration.sh   (another Odoo image)
#        ODOO_IMAGE=master scripts/integration.sh      (build Odoo master from the nightly .deb)
set -eu
cd "$(dirname "$0")/.."
if [ "${ODOO_IMAGE:-}" = master ]; then
  docker build -q ${MASTER_BUILD_FLAGS---no-cache} -t odoo-community-mcp-it/odoo:master -f tests/integration/odoo-master.Dockerfile tests/integration >/dev/null
  export ODOO_IMAGE=odoo-community-mcp-it/odoo:master
fi
compose="docker compose -f tests/integration/compose.yaml"
port="${ODOO_PORT:-18069}"
export ODOO_PORT="$port"

cleanup() { [ "${KEEP_ODOO:-0}" = 1 ] || $compose down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

$compose up -d --wait db
$compose run --rm odoo odoo -d mcptest -i base --stop-after-init --no-http
$compose up -d odoo

seed=$($compose exec -T odoo odoo shell -d mcptest --no-http \
  --db_host db --db_user odoo --db_password odoo < tests/integration/seed.py | sed -n 's/^SEED_RESULT //p')
[ -n "$seed" ] || { echo "seeding failed" >&2; exit 1; }

i=0
until curl -fsS "http://127.0.0.1:$port/web/health" >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -lt 60 ] || { echo "Odoo did not become healthy" >&2; exit 1; }; sleep 2
done

if [ -n "${RUNNER:-}" ]; then
  ODOO_IT_URL="http://127.0.0.1:$port" ODOO_IT_SEED="$seed" $RUNNER pytest -m integration -p no:cacheprovider tests/integration
else
  docker run --rm --network host -v "$(pwd)":/app -w /app \
    -v "${XDG_CACHE_HOME:-$HOME/.cache}/odoo-community-mcp-dev":/cache -u "$(id -u):$(id -g)" \
    -e HOME=/cache/home -e UV_CACHE_DIR=/cache/uv -e UV_PROJECT_ENVIRONMENT=/cache/venv \
    -e UV_PYTHON_DOWNLOADS=never \
    -e ODOO_IT_URL="http://127.0.0.1:$port" -e ODOO_IT_SEED="$seed" \
    "$(sed -n 's/^  \(ghcr.io\/astral-sh\/uv:[^ ]*\) \\$/\1/p' scripts/dev.sh)" \
    uv run pytest -m integration -p no:cacheprovider tests/integration
fi
