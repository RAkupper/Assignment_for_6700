# Changelog

What changed for users in each release. The version lives in `pyproject.toml`;
each release is a Git tag (`v0.2.0`) and an image in the GitHub Container Registry
under the same tag. How to cut and deploy one: [docs/deployment.md](docs/deployment.md#release-and-deploy).

## 0.6.1

### Fixed
- Answers took twice as long after 0.6.0 (median 41.1 s against 23.0 s): Grafana,
  idle, used 90% of a CPU that llama.cpp needs. Grafana and Prometheus are now capped
  at a quarter of a CPU each, Grafana gets 512 MB, and its plugin downloads and its
  own alerting are off. With them stopped, two slow test questions took 12 s instead
  of 32 to 37 s ([postmortem](docs/postmortems/2026-11-25-monitoring-slowed-answers.md)).
- `/metrics` was public after 0.6.0: the Caddyfile was mounted as a single file, so
  Caddy kept the old one after `git checkout`. `caddy/` and `monitoring/` are now
  mounted as folders, and `scripts/deploy.sh` reloads Caddy and Prometheus.
- The SLO allows 45 s, not 30 s: 0.5.0 already took 23 s at the median and 34 s at
  most on TEAM02, one question at a time.
- 0.5.0's entry said answers got about 2 s slower; on TEAM02 it was 9 s.

### Changed
- `deploy.log` records the test set's median time next to its score.

## 0.6.0

### Added
- Logs are JSON lines through loguru, one `request` line per request with its route,
  status and time, plus `retrieval` and `model call` lines with passages, model,
  time and tokens. Questions, answers, keys and client addresses are never logged.
- Every response has an `X-Request-ID`; every log line for that request carries it.
- `/metrics` for Prometheus: requests, failures and time per route, `/ask` outcomes
  (including the empty-retrieval rate), model call time and failures, tokens per
  answer, retrieval time. Caddy does not serve it.
- A `monitoring` Compose profile with Prometheus and Grafana, neither public, and a
  provisioned dashboard. llama.cpp now exposes its own metrics.
- An SLO for `/ask`: 95% of requests over 7 days answered without a server error
  within 30 s, with recording rules and burn-rate alerts
  ([observability.md](docs/observability.md)).

### Changed
- The page and its scripts are cached per release: `/assets/app.js?v=0.6.0` is kept
  for a year, the page itself is revalidated on every visit, and answers stay
  `no-store`. A new release loads new code without a hard refresh.
- Caddy starts only once the app is healthy, so the first visits after a deploy get
  the page instead of a 502.
- Network failures on the page say "Could not reach the service" in every browser
  (Chrome's "Failed to fetch", Safari's "Load failed" and "Fetch is aborted").
- `print` is gone from `src/`: command-line tools log through loguru too, and ruff's
  `T20` rule keeps it that way. The unused `myapp.main` placeholder is removed.

## 0.5.0

### Changed
- Answers cite their sources inline: each sentence ends with [1], [2] for the
  passage it comes from. The prompt now lists the evidence as numbered passages
  instead of a JSON block, asks for 2 to 4 sentences with every relevant source,
  and puts the question last (#57, cause 4).
- The prompt states the visa chosen on the page, and the app flags F, M or J
  classes that do not exist (an "F-4" question) so the model says so instead of
  answering yes (#47.10).
- Measured on the test set with the same model and data: inline citations 0 to 19
  of 20 answers, all cases 22 to 23 of 25 (f4-stem-opt now passes), no case
  regressed. Answers are a sentence or two longer, and on TEAM02 that costs more
  than measured locally: the median answer went from 13.6 s to 23.0 s, the
  slowest from 24.5 s to 34.3 s.

## 0.4.0

### Changed
- A cross-encoder reranker (MiniLM-L6) rereads the top 20 hybrid-search candidates
  with the question and decides their order and relevance, instead of a single dense
  cosine cutoff (#57, cause 3). On the 25-question test set, retrieval alone: the
  right source in the top 3 went from 15 to 17 of 20, ranked first from 13 to 15,
  and vague or off-topic questions declined from 3 to 4 of 4. "What does Form I-983
  relate to?" is now answered instead of declined, and the M-1 work rule now comes
  first for "Can an M-1 student work while studying?". Deployed on TEAM02, the full
  test set went from 19 to 22 of 25 and the median answer from 18.4 s to 13.6 s,
  although the reranker itself costs about 3 s of CPU per question
  ([docs/evaluation.md](docs/evaluation.md#results)).
- New settings `SEARCH_RERANK_MODEL` (empty turns it off), `SEARCH_RERANK_CANDIDATES`
  and `SEARCH_MIN_RERANK`. A hit's `score` is now the reranker's score.

## 0.3.0

### Added
- Backups of the catalog, the other files in `/data` and a Qdrant snapshot, checked
  against a manifest of sha256 hashes and counts. Each one is uploaded as a
  `data-<UTC time>` release in this repo, off Jetstream2 (#55).
- A verified restore: nothing is replaced until every file, the catalog and the
  snapshot's point count match the manifest, and any failure puts everything back
  (#55).

### Documentation
- The runbook covers taking a backup, uploading it by hand while there is no token,
  restoring it, and a restore drill on empty volumes (#55).

## 0.2.1 (2026-11-24)

### Changed
- Every request to Qdrant gives up after `QDRANT_TIMEOUT_S` (5 s; 60 s in the jobs
  container) instead of relying on the client library's default. A search takes
  well under 0.2 s, so a stuck Qdrant now fails fast with a 503 and `Retry-After`
  (#66).

### Documentation
- A runbook with the exact commands and real outputs for each routine task (#63).
- The final architecture diagrams, including where each time limit sits (#64).
- A postmortem on the app's link breaking each time TEAM02's IP changed (#65).

## 0.2.0 (2026-11-24)

The walking skeleton becomes a service you can reach, ship and roll back.

### Added
- HTTPS at https://team02.cis240466.projects.jetstream-cloud.org through Caddy, which
  keeps working when the instance's IP changes (#51, #59).
- Rate limits: 10 `/ask` and 30 `/retrieve` a minute per client, then 429 with
  `Retry-After` (#51).
- When the model is down or busy, `/ask` still returns the matching passages, with
  status `model_unavailable` and a plain message on the page (#56).
- Each push to `main` publishes the image to GHCR after a smoke test, and each
  version tag publishes it under that version (#61).
- `scripts/deploy.sh <version>` deploys a release and rolls back to an older one,
  and logs each deploy.
- A fixed set of 25 questions with known answers, run after every deploy (#58).

### Changed
- `/generate` moved to `/admin/generate` and needs the `X-Admin-Key` header (#51).
- The app's port 8000 is no longer reachable from outside; use the HTTPS address (#51).
- Time limits are ordered so the user always gets an answer or a clear message: 60 s
  waiting for a model slot, 120 s for the model, 200 s at the proxy, 210 s in the page
  (#56).
- Up to two questions are answered at once, matching the model server's slots (#43).
- Source citations link to the regulation on eCFR, and answers point to the DSO (#45).
- Data lives in named Docker volumes (#54).
- `/health` reports the release version.

### Removed
- `/predict` and the digits classifier from Homework 2 (#45).

## 0.1.0 (2026-10-28)

The walking skeleton for Deliverable 2: `/ask` answers questions about 8 CFR 214
from eCFR with cited passages, filtered by visa type and date; a web page at `/`;
`/retrieve`, `/health` and `/ready` (#41).
