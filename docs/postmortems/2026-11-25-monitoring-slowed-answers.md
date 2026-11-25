# 2026-11-25: answers took twice as long, and /metrics was public, after 0.6.0

## What users saw

After 0.6.0 went live, every answer took about twice as long: the test set's median
went from 23.0 s to 41.1 s and the slowest from 34.3 s to 77.9 s. Quality did not
change (23 of 25 both times). Answers over 60 s wait long enough that most people
would give up. For about 10 minutes, `/metrics` was also reachable through the public
address. It holds request counts and timings, never questions or answers.

## How we found out

The test set that `scripts/deploy.sh` runs after every deploy printed 40 to 60 s per
question where the day before it printed 14 to 32 s. `/metrics` answered 200 to a
`curl` from a laptop that should have got 404.

## Timeline (Eastern)

- 16:10 0.6.0 is up with Prometheus and Grafana; the test set starts.
- 16:14 the first five answers take 40 to 61 s; the deploy is left to finish.
- 16:15 `/metrics` answers 200 from outside. Recreating the Caddy container makes
  it 404.
- 16:27 the test set ends: 23/25, median 41.1 s.
- 16:30 `docker stats` shows Grafana at 90% of a CPU with nobody using it. The same
  two questions take 32.2 s and 36.5 s with Grafana and Prometheus running, and 12.7 s
  and 12.3 s with them stopped.

## Why it happened

Slow answers:

- llama.cpp writes answers on both of TEAM02's two CPUs, so anything else busy slows
  every answer, and retrieval with it.
- Grafana, idle, used 90% of a CPU. Its 256 MB memory cap was nearly full (193 MB),
  and at start it also tries to download plugins and runs its own alerting, neither
  of which we use.
- Nothing limited how much CPU Grafana or Prometheus could take.

Public `/metrics`:

- The Caddyfile was mounted as a single file. `git checkout` replaces the file, and a
  single-file mount keeps showing the old one, so Caddy never saw the new rule that
  refuses `/metrics`.
- The deploy did not recreate or reload Caddy, because nothing in its Compose
  settings changed.

## What went well

- The test set after every deploy caught the slowdown on the first questions.
- The new metrics and `docker stats` pointed at Grafana within minutes, and stopping
  it confirmed the cause before changing anything.
- Retrieval and the model were never wrong, only slow, and nothing had to be rolled
  back.

## What we'll change

- [x] Cap Grafana and Prometheus at a quarter of a CPU each, give Grafana 512 MB, and
  turn off its plugin downloads and alerting (0.6.1, #60).
- [x] Mount the `caddy/` and `monitoring/` folders instead of single files, and have
  `scripts/deploy.sh` reload Caddy and Prometheus after every deploy (0.6.1, #60).
- [x] Measure speed on TEAM02, not on a laptop: 0.5.0's "about 2 s slower" was a local
  number; TEAM02 showed 9 s (0.6.1 corrects the changelog).
- [x] Put the test set's median time in the deploy log line, so a slowdown shows in
  `deploy.log` without reading the full report (0.6.1).
