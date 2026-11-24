#!/bin/sh
# Start the image hardened (read-only, no capabilities) and check its HTTP contract.
# Usage: scripts/image-smoke-test.sh <image>
set -eu
image="$1"
name="odoo-mcp-smoke-$$"
port="${SMOKE_PORT:-18000}"

docker run -d --name "$name" --read-only --cap-drop ALL --security-opt no-new-privileges \
  -e ODOO_URL=https://odoo.example.com -e ODOO_DB=example -e ODOO_MODELS=res.partner \
  -p "127.0.0.1:$port:8000" "$image" >/dev/null
trap 'docker logs "$name" >&2 || true; docker rm -f "$name" >/dev/null 2>&1 || true' EXIT

i=0
until curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -lt 30 ] || { echo "container did not start" >&2; exit 1; }; sleep 1
done

user=$(docker inspect --format '{{.Config.User}}' "$image")
[ "$user" != "" ] && [ "$user" != "0" ] && [ "${user%%:*}" != "root" ] || { echo "image runs as root" >&2; exit 1; }
if docker run --rm --entrypoint sh "$image" -c true >/dev/null 2>&1; then
  echo "image unexpectedly contains a shell" >&2; exit 1
fi
status=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$port/mcp")
[ "$status" = 401 ] || { echo "expected 401 without bearer, got $status" >&2; exit 1; }
echo "smoke test passed (user $user)"
