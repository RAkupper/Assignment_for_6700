#!/usr/bin/env bash
# Deploy a release, or roll back by deploying an older one: the same command.
# Usage, on TEAM02 from the repo root: scripts/deploy.sh v0.2.0
#
# 1. Check out the release tag, so compose.yml and the Caddyfile match the image.
# 2. Pull ghcr.io/rakupper/assignment_for_6700:<version> and record its digest.
# 3. Pin that digest in .env as APP_IMAGE, so restarts and reboots keep it.
# 4. Run pending catalog migrations (each backs the catalog up first).
# 5. Start the stack on the image, wait for /ready, check /health reports <version>.
# 6. Run the evaluation set against it (SKIP_EVAL=1 skips, e.g. in an emergency).
# 7. Append one line to deploy.log: time, version, digest, result, details.
set -euo pipefail

REPO=ghcr.io/rakupper/assignment_for_6700
LOG=deploy.log
BASE=http://127.0.0.1:8000  # APP_BIND: the app answers only on this machine

main() {  # a function, so bash has read all of it before git checkout changes the file
  local version="${1:?usage: scripts/deploy.sh <version, e.g. v0.2.0>}"
  [[ "$version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "not a release tag: $version"
  [[ -f .env ]] || die "run from the repo root, next to .env"
  git diff --quiet HEAD -- || die "the checkout has local changes; commit or stash them first"

  local previous
  previous=$(last_good_version)
  echo "deploying $version (last good: ${previous:-none})"

  VERSION=$version
  git fetch --quiet --tags origin
  git -c advice.detachedHead=false checkout --quiet "$version" ||
    fail "no tag $version on GitHub" "$previous"

  docker pull --quiet "$REPO:$version" >/dev/null ||
    fail "could not pull $REPO:$version (did CI publish it?)" "$previous"
  DIGEST=$(docker inspect --format '{{index .RepoDigests 0}}' "$REPO:$version")
  echo "image $DIGEST"
  if grep -q '^APP_IMAGE=' .env; then
    sed -i "s|^APP_IMAGE=.*|APP_IMAGE=$DIGEST|" .env
  else
    echo "APP_IMAGE=$DIGEST" >> .env
  fi

  docker compose run --rm --no-deps -T jobs python -m myapp.ingest.catalog migrate ||
    fail "catalog migration failed" "$previous"
  docker compose up -d --no-build --wait --wait-timeout 300 ||
    fail "the stack did not become healthy" "$previous"

  local ready=""
  for _ in $(seq 60); do  # the retrieval index and models load after the port opens
    curl -fsS -m 5 "$BASE/ready" >/dev/null 2>&1 && { ready=yes; break; }
    sleep 2
  done
  [[ -n "$ready" ]] || fail "/ready did not answer 200 within 2 minutes" "$previous"
  curl -fsS -m 5 "$BASE/health" | grep -q "\"version\":\"${version#v}\"" ||
    fail "/health does not report ${version#v}" "$previous"

  local result="eval skipped"
  if [[ -z "${SKIP_EVAL:-}" ]]; then
    echo "running the evaluation set (about 10 minutes)"
    local report
    report=$(docker compose run --rm --no-deps -T jobs python -m myapp.evaluation \
      --base-url http://app:8000 --out "/data/eval-$version-$(date -u +%Y%m%dT%H%M%SZ).json" \
      2>&1 | tee /dev/stderr) || true  # tee: show progress while it runs
    local score
    score=$(grep -o 'All cases passed | [0-9]*/[0-9]*' <<<"$report" | grep -o '[0-9]*/[0-9]*$') ||
      { echo "$report" | tail -5; score="did not run"; }
    result="eval $score"
  fi

  record ok "$result"
  echo "deployed $version: $result"
}

last_good_version() {  # the version of the newest "ok" line in deploy.log
  [[ -f "$LOG" ]] && awk '$4 == "ok" { v = $2 } END { print v }' "$LOG"
  return 0
}

record() {  # record <result> <details>: one line in deploy.log
  printf '%s %s %s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${VERSION:-?}" \
    "${DIGEST:-?}" "$1" "$2" >> "$LOG"
}

fail() {  # fail <reason> <previous version>: log it and say how to roll back
  record failed "$1"
  echo "FAILED: $1" >&2
  docker compose logs --tail 20 app >&2 || true
  [[ -n "$2" ]] && echo "roll back with: scripts/deploy.sh $2" >&2
  exit 1
}

die() {
  echo "$1" >&2
  exit 2
}

main "$@"; exit  # on one line, so bash never reads past here after the checkout
