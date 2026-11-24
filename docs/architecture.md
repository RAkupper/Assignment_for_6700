# Architecture

The system as built, revised from Deliverable 2. Each part carries its status on
Nov 24: **built**, **in progress** (open work in #60 or #55), or **not built**.
The diagrams are SVG and PNG in [architecture/](architecture/); to change one, open
`architecture/architecture.drawio` in [draw.io](https://app.diagrams.net) (two pages,
one per diagram) and export both formats again.

## Solution architecture: process, execution, state

![Solution architecture](architecture/solution-architecture.svg)

- **Process.** We build the data and ship releases; students and DSOs ask questions
  and read the evidence; we keep it running with health checks, the evaluation set
  after every deploy, and one-command deploys and rollbacks.
- **Execution.** One page at `/`, an HTTPS API behind Caddy, and a backend that
  retrieves, gates on relevance, waits for a model slot and generates. When the model
  is down or busy, the user still gets the matching passages.
- **State.** Code and releases live in GitHub and GHCR; data, models and the
  certificate live in named Docker volumes on TEAM02's 40 GB volume; backups go to
  GitHub releases, off the instance (#55).

## Technical architecture: components, data flow, deployment

![Technical architecture](architecture/technical-architecture.svg)

Only Caddy is reachable from the internet, on 80 and 443. The app listens on
127.0.0.1:8000, and Qdrant (6333) and llama.cpp (8080) are not published at all. CI
builds and smoke-tests every commit and publishes images to GHCR;
`scripts/deploy.sh <version>` pulls a release by tag and pins its digest on TEAM02
([runbook](runbook.md#deploy-a-release)).

## Where each time limit sits

Each layer gives up after the one inside it, so the user always gets an answer or a
clear message instead of a page that hangs.

| Layer | Gives up after | Then the user sees | Set in |
| --- | --- | --- | --- |
| Page waiting for `/ask` | 210 s | "The answer took too long" | `web/app.js` |
| Caddy waiting for the app | 200 s (connect 5 s) | 504, "The answer took too long" | `Caddyfile` |
| Waiting for one of 2 model slots | 60 s | the passages, status `model_unavailable` | `GENERATION_QUEUE_TIMEOUT_S` |
| Model call, retries included | 120 s (connect 5 s) | the passages, status `model_unavailable` | `LLM_TIMEOUT_S`, `LLM_CONNECT_TIMEOUT_S` |
| Qdrant search | 5 s, the client library's default | 503 with `Retry-After` | not set explicitly yet |
| eCFR download (jobs only) | 30 s per request, retried with backoff | the job fails and can be rerun | `INGEST_REQUEST_TIMEOUT_S` |

The slowest normal answer takes about 48 s (two questions at once on two CPUs), so
60 s + 120 s leaves room for a full retry budget while staying under Caddy's 200 s.
Measurements are in [deployment.md](deployment.md#time-limits-and-retries).

## What changed since Deliverable 2

| Added or changed | Why |
| --- | --- |
| Caddy in front of the app, HTTPS at the instance's hostname | Encrypted traffic; the hostname survives IP changes on unshelve (#51, #59) |
| App bound to 127.0.0.1, rate limits, `/admin` behind a key | Only what users need is public (#51) |
| Ordered time limits, retries, evidence-only fallback | A broken model gives a clear answer instead of a hung page (#56) |
| Named Docker volumes | The same start on any machine; no host paths (#54) |
| GHCR images, version tags, `scripts/deploy.sh` | Ship and roll back with one command, with a deploy log (#61, #62) |
| Evaluation set run after every deploy | Notice answers getting worse even when every status is 200 (#58) |
| Answer cache dropped from the design | Questions describe a user's visa status, so answers are not cached anywhere (`Cache-Control: no-store`) |
| Backups off the instance, JSON logs, `/metrics` with Grafana | In progress (#55, #60) |
