# Changelog

What changed for users in each release. The version lives in `pyproject.toml`;
each release is a Git tag (`v0.2.0`) and an image in the GitHub Container Registry
under the same tag. How to cut and deploy one: [docs/deployment.md](docs/deployment.md#release-and-deploy).

## 0.2.0

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
