# Observability

How to tell whether the service works and, when it doesn't, which request failed
and where. Logs say what happened to one request; metrics say how all of them are
doing; the SLO says whether that is good enough.

## Logs

The app writes one JSON object per line to stderr (`APP_LOG_JSON=true`, the
production default; `.env.local.example` turns it off for plain text). Every line
written while a request is handled carries that request's ID:

```json
{"time": "2026-11-25T20:47:22.010+00:00", "level": "INFO", "logger": "myapp.generation.service", "message": "retrieval", "request_id": "demo-1", "hits": 5, "duration_ms": 412}
{"time": "2026-11-25T20:47:23.528+00:00", "level": "INFO", "logger": "myapp.model_client", "message": "model call", "request_id": "demo-1", "model": "qwen2.5-1.5b-instruct", "duration_ms": 1518, "completion_tokens": 6}
{"time": "2026-11-25T20:47:23.530+00:00", "level": "INFO", "logger": "myapp.app", "message": "request", "request_id": "demo-1", "method": "POST", "route": "/ask", "status": 200, "duration_ms": 1931, "answer_status": "answered"}
```

| Line | Fields |
| --- | --- |
| `request`, one per request | method, route, status, duration, and for `/ask` the answer's outcome |
| `retrieval` | passages returned, time to embed, search and rerank |
| `model call` | model name, time including retries, tokens the model wrote |
| warnings | model retries, answers given with passages only |

Logs never contain a question, an answer, a prompt, a key or a client address.
`/health` and `/metrics` are counted but not logged, since they are polled every few
seconds. uvicorn's own access log is off; the `request` line replaces it.

**Request IDs.** Each response has an `X-Request-ID` header. A caller can send its
own (letters, digits, `.`, `_`, `-`, up to 64 characters) and gets the same one
back; anything else is replaced with a new random ID. To follow one request:

```bash
docker compose logs app | grep '"request_id": "demo-1"'
docker compose logs app --no-log-prefix | jq 'select(.status >= 500)'  # server errors
```

Command-line tools (`myapp.ingest`, `myapp.vectordb.loader`, `myapp.evaluation`)
log plain text: output on stdout, warnings and errors on stderr. No code in
`src/` calls `print`; ruff's `T20` rule enforces it.

## Metrics

`GET /metrics` serves Prometheus metrics. It is not public: Caddy answers 404 for
that path, and the app's port is bound to 127.0.0.1. Prometheus reaches it over the
Compose network.

| Metric | Type | What it answers |
| --- | --- | --- |
| `app_requests_total{route, status}` | counter | Traffic and failures per route |
| `app_request_duration_seconds{route}` | histogram | How long each route takes |
| `app_answers_total{status}` | counter | `/ask` outcomes; `insufficient_evidence` is the empty-retrieval count |
| `app_ask_slo_events_total{good}` | counter | `/ask` requests judged against the SLO |
| `app_model_call_duration_seconds` | histogram | Time in the model server per answer |
| `app_model_call_failures_total{kind}` | counter | `timeout`, `unavailable` or `bad_response` |
| `app_answer_tokens` | histogram | Tokens written per answer, from the server's usage report |
| `app_retrieval_duration_seconds` | histogram | Embed, search and rerank time |

Routes are labelled by their template (`/ask`), never by the raw path; probes of
unknown paths share one `unmatched` label, so a scanner can't create new series.
llama.cpp (`--metrics`) adds busy and waiting slots and tokens per second, and
Qdrant adds its own collection and request metrics.

## Prometheus and Grafana

Both run with the `monitoring` Compose profile. On TEAM02, `.env` has
`COMPOSE_PROFILES=proxy,monitoring`, so `docker compose up -d` starts them with the
rest. Prometheus scrapes `app`, `llm` and `qdrant` every 15 s and keeps 30 days.
Neither is public: Prometheus has no published port, and Grafana listens on the
instance's 127.0.0.1 only. Each may use a quarter of a CPU at most: llama.cpp needs
both of TEAM02's CPUs, and an uncapped, idle Grafana once doubled answer times
([postmortem](postmortems/2026-11-25-monitoring-slowed-answers.md)). To open the dashboard from a laptop:

```bash
ssh -L 3000:127.0.0.1:3000 exouser@team02.cis240466.projects.jetstream-cloud.org
# then open http://localhost:3000
```

The **Student Visa Guide** dashboard opens first. It is provisioned from
`monitoring/grafana/dashboards/rag-service.json`, so changes go through a pull
request, not the Grafana editor. Viewing needs no login; the admin login is
`admin` with `GRAFANA_ADMIN_PASSWORD` from `.env`.

| Row | Panels |
| --- | --- |
| Is it working? | SLO over 7 days, error budget left, requests per minute, targets up |
| Traffic | Requests per minute by route; 4xx and 5xx per minute |
| Speed | `/ask` p50 and p95; model call p50 and p95; retrieval p50 and p95 |
| Answers | Outcomes per minute; empty-retrieval rate; tokens per answer |
| Model server | Failures by kind; llama.cpp slots busy and waiting; tokens per second |

## SLO for `/ask`

**95% of `/ask` requests in any 7 days get a written or not-covered answer, without
a server error, within 45 seconds.**

A request is **good** when the status is 2xx, the answer's status is `answered` or
`insufficient_evidence`, and it finished within 45 s. It is **bad** when the status
is 5xx, the model failed and only passages came back (`model_unavailable`), or it
took longer than 45 s. 4xx responses (invalid input, the rate limit) are left out:
they say nothing about whether the service works.

Why these numbers: on TEAM02's two CPUs, one question at a time, the median answer
takes 23 s and the slowest 34 s (v0.5.0 test set, [evaluation.md](evaluation.md#results)).
Two at once take longer, up to about 48 s
([deployment.md](deployment.md#time-limits-and-retries)). 45 s covers every single
answer with room to spare and most pairs; past that, users give up. 95% leaves
room for a burst of three or more users, which queues behind the two answer slots.
That leaves a budget of 5% bad requests per 7 days.

| | PromQL |
| --- | --- |
| SLI | `sum(increase(app_ask_slo_events_total{good="true"}[7d])) / sum(increase(app_ask_slo_events_total[7d]))` |
| Recorded as | `app:ask_slo_good_ratio:rate7d` (also `:rate1h`, `:rate5m`) in `monitoring/rules.yml` |
| Budget left | `1 - (1 - app:ask_slo_good_ratio:rate7d) / 0.05` |

`monitoring/rules.yml` also defines three alerts, shown under Alerts in Prometheus
and Grafana (there is no Alertmanager, so nobody is paged):

| Alert | Fires when |
| --- | --- |
| `AskSLOBurningFast` | Over 30% of `/ask` requests were bad in both the last hour and the last 5 minutes: six times the budget rate, gone in about a day |
| `AppDown` | Prometheus cannot reach the app for 2 minutes |
| `ModelServerDown` | Prometheus cannot reach llama.cpp for 5 minutes |

When an alert fires, start with the runbook: [runbook.md](runbook.md).
