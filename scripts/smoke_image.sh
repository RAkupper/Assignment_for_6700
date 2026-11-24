#!/usr/bin/env bash
# Start a freshly built image on its own and check it before anyone trusts it.
# Usage: scripts/smoke_image.sh <image>
# CI runs this before pushing to GHCR. The app starts on its defaults (echo model,
# retrieval off), so no model server or Qdrant is needed.
set -euo pipefail

image="${1:?usage: scripts/smoke_image.sh <image>}"
port="${SMOKE_PORT:-8000}"
base="http://127.0.0.1:$port"
name="smoke-$$"

fail() {
  echo "FAIL: $*" >&2
  docker logs "$name" 2>&1 | tail -20 >&2 || true
  exit 1
}
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT

[[ "$(docker inspect --format '{{json .Config.Healthcheck}}' "$image")" != "null" ]] ||
  fail "the image has no HEALTHCHECK"

docker run -d --name "$name" -p "127.0.0.1:$port:8000" "$image" >/dev/null
for _ in $(seq 30); do  # the app needs a few seconds to import and start
  curl -fsS "$base/health" >/dev/null 2>&1 && break
  sleep 1
done

curl -fsS "$base/health" | grep -q '"status":"ok"' || fail "/health did not answer ok"
curl -fsS "$base/" | grep -q "<title>" || fail "the page at / did not load"
curl -fsS -o /dev/null "$base/docs" || fail "/docs did not load"
[[ "$(docker exec "$name" id -u)" == "10001" ]] || fail "the app does not run as appuser"
leaked=$(docker exec "$name" find /app -maxdepth 2 \( -name '.env*' -o -name .git \))
[[ -z "$leaked" ]] || fail "found in the image: $leaked"

echo "PASS: $image answers /health, /, /docs, runs as appuser, carries no .env or .git"
