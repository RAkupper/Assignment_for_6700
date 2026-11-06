#!/usr/bin/env bash
# Usage, once, from the repo root after pulling #52: scripts/migrate_to_volumes.sh /media/volume/team02-data
set -euo pipefail

src="${1:?usage: scripts/migrate_to_volumes.sh <old DATA_VOLUME_DIR>}"
for dir in data embed-cache qdrant qdrant-snapshots; do
  [[ -d "$src/$dir" ]] || { echo "missing $src/$dir" >&2; exit 1; }
done

catalog_check="import sqlite3; c = sqlite3.connect('/data/catalog.sqlite'); print('catalog', c.execute('PRAGMA integrity_check').fetchone()[0], c.execute('SELECT count(*) FROM provision_versions').fetchone()[0], 'provision versions')"

check() {  # check [docker compose run options]: alias, collections, point counts, catalog
  docker compose run --rm --no-deps -T "$@" jobs python -m myapp.vectordb.collection status
  docker compose run --rm --no-deps -T "$@" jobs python -c "$catalog_check"
}

copy() {  # copy <service> <folder under src> <path in container> [owner]
  docker compose run --rm --no-deps -T --user 0 --entrypoint sh -v "$src/$2:/from:ro" "$1" \
    -c "cp -a /from/. '$3'/ ${4:+&& chown -R $4 '$3'}"
}

docker compose build app jobs  # up below must start the new app, not the old image
before=$(mktemp)
after=$(mktemp)
check -v "$src/data:/data" > "$before"  # --no-deps keeps the old qdrant container running
cat "$before"

docker compose stop app qdrant  # nothing may write while the files are copied
copy jobs data /data 10001:10001
copy jobs embed-cache /embed-cache 10001:10001
copy qdrant qdrant /qdrant/storage
copy qdrant qdrant-snapshots /qdrant/snapshots

docker compose up -d --wait
check > "$after"
cat "$after"
if diff "$before" "$after"; then
  echo "migrated: same alias, collections, point counts and catalog"
else
  echo "MISMATCH: the old folders under $src are untouched" >&2
  exit 1
fi
