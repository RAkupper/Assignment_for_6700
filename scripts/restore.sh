#!/usr/bin/env bash
# Usage, from the repo root: scripts/restore.sh [latest | data-<UTC time>] [--no-activate]
set -euo pipefail
cd "$(dirname "$0")/.."  # works from any folder
docker compose up -d --wait qdrant
exec docker compose run --rm -T jobs python -m myapp.backup restore "${@:-latest}"
