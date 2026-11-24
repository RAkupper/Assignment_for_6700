#!/usr/bin/env bash
# Usage, from the repo root: scripts/backup.sh (needs BACKUP_GITHUB_TOKEN in .env.backup)
set -euo pipefail
cd "$(dirname "$0")/.."  # systemd runs it from anywhere
exec docker compose run --rm -T jobs python -m myapp.backup create --upload "$@"
