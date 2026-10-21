# Assignment_for_6700

A FastAPI service for our DSAN 6700 project. It has two models: a scikit-learn digit
classifier that runs inside the app (`POST /predict`), and a small language model
(Qwen2.5-1.5B-Instruct) that runs in its own llama.cpp container (`POST /generate`).
Everything starts with `docker compose up -d` on our Jetstream2 instance.

## Contents

- [Quickstart](#quickstart) (tutorial: clean machine to running service)
- [Deploy with Compose](#deploy-with-compose) (run the full stack on an instance and try every endpoint)
- [Docker](#docker) (build, run, and check the container)
- [How-to guides](#how-to-guides) (tests, linting, pre-commit, adding dependencies)
- [Reference](#reference) (endpoints, project layout, CI pipeline)
- [Explanation](#explanation) (why the project is structured this way)

## Quickstart

### 1. Prerequisites

- Git
- [uv](https://docs.astral.sh/uv/) (installs and manages Python for you, so you do
  not need to install Python 3.13 yourself)

Install uv if you do not have it:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Restart your terminal, then confirm it works:

```bash
uv --version
```

### 2. Clone and install

```bash
git clone https://github.com/RAkupper/Assignment_for_6700.git
cd Assignment_for_6700
uv sync --extra dev --frozen
```

`--frozen` installs the exact versions pinned in `uv.lock`, so every teammate and
the CI runner get the same environment. `--extra dev` adds ruff, pytest, mypy and
pre-commit.

### 3. Run the service

To use the local development settings, copy the example to `.env`:

```bash
cp .env.local.example .env
```

Then start the service:

```bash
uv run uvicorn myapp.app:app --reload
```

You should see `Uvicorn running on http://127.0.0.1:8000`.

### 4. Check that it is healthy

In a second terminal:

```bash
curl http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok","version":"0.1.0"}
```

Interactive API docs are at <http://127.0.0.1:8000/docs>. Press `Ctrl+C` to stop the
server.

## Deploy with Compose

We run the whole stack on our Jetstream2 instance (TEAM02) with Docker Compose. The
same steps should work on any Linux machine with Docker. More details and our
measurements are in [docs/deployment.md](docs/deployment.md).

### 1. Prerequisites

- Docker and Docker Compose 2.24 or newer. Check with:

  ```bash
  docker compose version
  ```

- Port 8000 open. On a Jetstream2 instance made in Exosphere, Docker is already
  installed and the port is open by default.
- About 4 GB of free disk (app image ~470 MB, llama.cpp image ~1.2 GB, model 1.1 GB).

### 2. Start the stack

```bash
git clone https://github.com/RAkupper/Assignment_for_6700.git
cd Assignment_for_6700
docker compose up -d --build
```

This builds the app image, starts the `llm` container (llama.cpp), and starts `app`
once `llm` is healthy. The first time, it also downloads the model (1.1 GB) into the
`llm-models` volume.

Settings come from `.env.production.example`. To change something, put it in a `.env`
file in the same folder. `.env` is gitignored and overrides the example.

Check that both containers say `healthy` (about 40 s the first time):

```bash
docker compose ps
```

### 3. Try the endpoints

Use `localhost` on the instance, or the instance's public IP from your own computer
(ours is `149.165.150.147`):

```bash
HOST=http://localhost:8000
```

Health check:

```bash
curl $HOST/health
```

```json
{"status":"ok","version":"0.1.0"}
```

Ready check (returns 503 if the model server isn't up yet):

```bash
curl $HOST/ready
```

```json
{"status":"ready"}
```

Predict a digit. `pixels` is an 8×8 image flattened to 64 numbers from 0 to 16, and
this one is a 0 from the scikit-learn digits dataset. It takes about 1 second because
we kept the `time.sleep(1)` from the blocking-call audit (see
[discussion.md](discussion.md)):

```bash
curl -X POST $HOST/predict \
  -H "Content-Type: application/json" \
  -d '{"pixels":[0,0,5,13,9,1,0,0,0,0,13,15,10,15,5,0,0,3,15,2,0,11,8,0,0,4,12,0,0,8,8,0,0,5,8,0,0,9,8,0,0,4,11,0,1,12,7,0,0,2,14,5,10,12,0,0,0,0,6,13,10,0,0,0]}'
```

```json
{"digit":0}
```

Generate text with the language model (answers are limited to 128 tokens):

```bash
curl -X POST $HOST/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "In one sentence, what is OPT for F-1 students?"}'
```

```json
{"completion":"OPT stands for Optional Practical Training, which allows F-1 students to gain practical work experience in their field of study."}
```

Wrong input gives a 422:

```bash
curl -i -X POST $HOST/predict \
  -H "Content-Type: application/json" \
  -d '{"pixels":[1,2,3]}'
```

You can also open `$HOST/docs` in a browser to try the endpoints.

### 4. Logs, stop, restart

```bash
docker compose logs -f app   # app logs
docker compose down          # stop (the downloaded model is kept)
docker compose up -d         # start again
```

Both containers have `restart: unless-stopped`, so they come back by themselves after
a reboot or an unshelve.

### 5. Switching the model server

The app finds the model server through `LLM_BASE_URL` and `LLM_MODEL`. For example,
to use the class model server instead, put this in `.env` and run
`docker compose up -d`:

```bash
LLM_BASE_URL=http://10.1.82.181:8000/v1
LLM_MODEL=Qwen/Qwen2.5-14B-Instruct-AWQ
```

See [docs/model-serving.md](docs/model-serving.md) for why we set it up this way.

## Docker

Build the application image:

```bash
docker build -t immigration-app:dev .
```

Run with configuration supplied at runtime:

```bash
docker run -d --name immigration-app-test \
  -p 127.0.0.1:8000:8000 \
  --env-file .env.production.example \
  immigration-app:dev
```

Check the service:

```bash
curl http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok","version":"0.1.0"}
```

See [Docker setup](docs/docker.md) for container health checks,
cleanup commands, image size, and rebuild measurements.

This only runs the app container. `/generate` and `/ready` also need the model server,
so use [Compose](#deploy-with-compose) to run everything.

## How-to guides

### Train and save the digits model

Run the training script separately from the API:

```bash
uv run python scripts/train_digits.py
```

The script trains an SVM classifier on scikit-learn's built-in 8×8 digits dataset,
evaluates it on a held-out 20% test split, and saves `models/digits.joblib`.
The split uses a fixed random seed for reproducibility. No dataset download or
GPU is needed. The model takes 64 pixel values (0–16) and predicts a digit (0–9).

The app loads this file once when it starts and uses it for `POST /predict`. Training
is not part of startup. `models/digits.joblib` is committed and copied into the Docker
image.

### Measure concurrent prediction requests

Run these commands from the project root. Start on your working branch with
local changes committed. Both `blocking-before` and `blocking-after` must be
available locally; if needed, download the tags with `git fetch origin --tags`.

In terminal 1, remember your branch, switch to the blocking version, install its
dependencies, and start one server worker:

```bash
audit_branch=$(git branch --show-current)
git switch --detach blocking-before
uv sync --extra dev --frozen
uv run uvicorn myapp.app:app --host 127.0.0.1 --port 8000 --workers 1
```

Wait for `Application startup complete`. In terminal 2, send ten concurrent
requests and save a new measurement without overwriting the original results:

```bash
uv run python scripts/benchmark_predict.py --output measurements/before-rerun.json
```

After the benchmark finishes, press `Ctrl+C` in terminal 1 to stop the server.
Switch to the fixed version and start the server again:

```bash
git switch --detach blocking-after
uv sync --extra dev --frozen
uv run uvicorn myapp.app:app --host 127.0.0.1 --port 8000 --workers 1
```

Wait for startup, then run the same benchmark in terminal 2:

```bash
uv run python scripts/benchmark_predict.py --output measurements/after-rerun.json
```

Stop the server with `Ctrl+C` in terminal 1, then return to your original branch
in that same terminal:

```bash
git switch "$audit_branch"
```

The tags are checked out in detached HEAD mode for inspection and testing; make
further changes on your working branch after returning to it.


### Run the tests

```bash
uv run pytest
```

With a coverage report:

```bash
uv run pytest --cov=myapp
```

### Lint, format and type-check

These are the same checks CI runs:

```bash
uv run ruff check            # lint
uv run ruff format --check   # verify formatting (drop --check to auto-format)
uv run mypy src/             # static type checking
```

Auto-fix most lint issues with `uv run ruff check --fix`.

### Enable the pre-commit hook (once per clone)

```bash
uv run pre-commit install
```

Every `git commit` now runs ruff (lint + format) plus basic hygiene checks (large
files, private keys, trailing whitespace, end-of-file newlines). If a hook changes a
file, stage the change and commit again. Run all hooks on every file manually with:

```bash
uv run pre-commit run --all-files
```

### Add a dependency

```bash
uv add <package>             # runtime dependency
uv add --optional dev <pkg>  # development tool
```

Commit both `pyproject.toml` and the updated `uv.lock` so teammates and CI stay in
sync.

## Reference

### Configuration

`Settings` in `src/myapp/config.py` reads environment variables and the
`.env` file in the working directory. Environment variables take precedence over
`.env`; missing values use the defaults below.

| Variable | Default | Meaning and validation |
| -------- | ------- | ---------------------- |
| `APP_NAME` | `assignment-for-6700` | Application title; must not be empty or whitespace-only. |
| `APP_ENVIRONMENT` | `development` | Must be `development` or `production`. |
| `APP_DEBUG` | `false` | Boolean debug flag; must be `false` in production. |
| `APP_MODEL_PATH` | `models/digits.joblib` | Saved digits model loaded at startup; must point to an existing file. |

Model settings use the `LLM_` prefix and are read by `ModelSettings` in the same
file. The defaults need no model server (`LLM_PROVIDER=echo`), so tests and CI run
anywhere.

| Variable | Default | Meaning and validation |
| -------- | ------- | ---------------------- |
| `LLM_PROVIDER` | `echo` | `echo` (returns the prompt) or `openai` (any OpenAI-compatible server: Ollama, vLLM, hosted API). Must not be `echo` in production. |
| `LLM_BASE_URL` | `http://localhost:11434/v1` | Server root including `/v1`; must be a valid URL. |
| `LLM_MODEL` | `qwen2.5:1.5b-instruct` | Model name as the server knows it. |
| `LLM_API_KEY` | unset | Bearer token for hosted APIs; kept secret in logs and reprs. |
| `LLM_TIMEOUT_S` / `LLM_CONNECT_TIMEOUT_S` | `120` / `5` | Seconds; must be positive. |
| `LLM_MAX_TOKENS` | `512` | Must be positive. |
| `LLM_TEMPERATURE` | `0.0` | Between 0 and 2. |
| `LLM_DEVICE` | `cpu` | `cpu`, `cuda` or `auto`; read by the model server. |
| `LLM_CACHE_DIR` | `/models` | Where the model server keeps weights (a mounted volume). |

These are the defaults in the code. With Compose, `compose.yml` and
`.env.production.example` set them for the llama.cpp container instead:
`LLM_PROVIDER=openai`, `LLM_BASE_URL=http://llm:8080/v1`,
`LLM_MODEL=qwen2.5-1.5b-instruct` and `LLM_MAX_TOKENS=128`.

Model failures reach clients as `504` (timeout), `503` (server unreachable or
erroring) or `502` (malformed reply). See
[docs/model-serving.md](docs/model-serving.md) for where the model runs and why.

### Run against a local model

```bash
ollama pull qwen2.5:1.5b-instruct     # once; needs https://ollama.com installed
LLM_PROVIDER=openai uv run uvicorn myapp.app:app --reload
LLM_PROVIDER=openai uv run python scripts/bench_model.py   # latency check
```


### Endpoints

| Method | Path      | Response model   | Description                              |
| ------ | --------- | ---------------- | ---------------------------------------- |
| GET    | `/health` | `HealthResponse` | Liveness check with service version     |
| GET    | `/ready` | `ReadyResponse` | Readiness check: the model server answers (`503`/`504` if not) |
| POST   | `/generate` | `GenerateResponse` | Model completion for a prompt |
| POST   | `/predict` | `PredictResponse` | Digit (0 to 9) for 64 pixel values (0 to 16) |
| GET    | `/docs`   | HTML             | Auto-generated Swagger UI (FastAPI)      |

`HealthResponse` fields:

| Field     | Type          | Example   |
| --------- | ------------- | --------- |
| `status`  | `"ok"`        | `"ok"`    |
| `version` | `str`         | `"0.1.0"` |

`/health` only says the app process is up, so a container can use it as a liveness
probe without depending on the model. `/ready` also checks that the model server
answers (a cheap `GET` on its `/models` listing, bounded by
`LLM_CONNECT_TIMEOUT_S`), so use it to see whether `/generate` can work:

```bash
curl http://127.0.0.1:8000/ready
```

`POST /generate` takes a `GenerateRequest` body and returns a `GenerateResponse`.
The model call runs in a worker thread, so `/health` keeps answering while a slow
generation is in flight.

| Model | Field | Type | Validation or example |
| --- | --- | --- | --- |
| `GenerateRequest` | `prompt` | `str` | Leading and trailing whitespace stripped; 1 to 4,000 characters, otherwise `422` |
| `GenerateResponse` | `completion` | `str` | `"OPT is a period of practical training..."` |

```bash
curl -X POST http://127.0.0.1:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is OPT?"}'
```

With the default `LLM_PROVIDER=echo` the completion is the prompt itself, which
confirms the wiring without a model server. Model failures return `504`, `503` or
`502` as described under [Configuration](#configuration).

`POST /predict` takes a `PredictRequest` and returns a `PredictResponse`. The model is
loaded at startup, and each prediction runs in a worker thread, so the 1 second delay
doesn't block other requests.

| Model | Field | Type | Validation or example |
| --- | --- | --- | --- |
| `PredictRequest` | `pixels` | `list[int]` | Exactly 64 integers from 0 to 16, row by row from an 8×8 image, otherwise `422` |
| `PredictResponse` | `digit` | `int` | 0 to 9, e.g. `0` |

There's a full curl example under [Deploy with Compose](#3-try-the-endpoints).

### Project layout

```text
Assignment_for_6700/
├── .github/
│   ├── ISSUE_TEMPLATE/        # bug report, documentation, feature request
│   └── workflows/ci.yml       # GitHub Actions quality gates
├── .env.local.example         # local development settings
├── .env.production.example    # deployed settings, read by compose at run time
├── .dockerignore              # keeps env files, .venv and .git out of the image
├── .pre-commit-config.yaml    # Git hooks (ruff, hygiene checks)
├── .python-version            # Python 3.13, read by uv and CI
├── Dockerfile                 # app image: uv sync --frozen, non-root, HEALTHCHECK
├── compose.yml                # app + llama.cpp model server, named volume, restarts
├── discussion.md              # blocking-call audit: step table and measurements
├── pyproject.toml             # metadata, dependencies, tool config
├── uv.lock                    # pinned, shared environment
├── src/myapp/
│   ├── __init__.py
│   ├── app.py                 # FastAPI app, Pydantic models, routes
│   ├── config.py              # typed Settings and ModelSettings from the environment
│   ├── healthcheck.py         # health check the container runs
│   ├── model_client.py        # ModelClient protocol, echo and OpenAI-compatible clients
│   └── main.py                # CLI entry-point placeholder
├── models/digits.joblib       # trained digits classifier loaded at startup
├── docs/
│   ├── deployment.md          # running the stack on Jetstream2, measurements
│   ├── docker.md              # building and checking the app image
│   └── model-serving.md       # where the model runs and why
├── measurements/              # before/after results of the blocking-call audit
├── outputs/docker/            # raw Docker build logs and timings
├── scripts/
│   ├── bench_model.py         # latency benchmark through the service's client
│   ├── benchmark_predict.py   # ten concurrent /predict requests for the audit
│   └── train_digits.py        # trains and saves models/digits.joblib
└── tests/
    ├── conftest.py, fakes.py  # FakeModelClient and its fast and slow fixtures
    ├── test_config.py         # Settings validation tests
    ├── test_generate.py       # /generate endpoint, health during a slow request
    ├── test_health.py         # /health endpoint tests
    ├── test_healthcheck.py    # container health check exit codes
    ├── test_model_client.py   # client lifecycle and dependency override
    ├── test_openai_client.py  # model settings, HTTP client, error mapping
    └── test_ready.py          # /ready endpoint and the client ping
```

### Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request, on `ubuntu-latest`:

1. Check out the repository
2. Install Python (version from `.python-version`)
3. Install uv and run `uv sync --extra dev --frozen`
4. `uv run ruff check` and `uv run ruff format --check`
5. `uv run mypy src/`
6. `uv run pytest`

A failure at any step fails the run. See results under the repository's
**Actions** tab.

## Explanation

- **src/ layout.** Tests import the installed package rather than files that happen
  to sit next to them, which catches packaging mistakes early.
- **pyproject.toml + hatchling.** One standard file declares the build backend,
  dependencies and tool settings; no `setup.py`.
- **uv.lock.** A committed lockfile makes installs reproducible across laptops, CI
  and the Docker image on our Jetstream2 instance.
- **Language model in its own container.** The app image only has the small digits
  model (91 KB). The 1.1 GB language model lives in a volume used by the llama.cpp
  container, so we can change the model with env vars instead of rebuilding.
- **Quality gates in two places.** pre-commit stops problems before they are
  committed; CI enforces the same checks for everyone, so the main branch stays
  green.

## Troubleshooting

- **`uv: command not found`**: restart your terminal after installing uv, or add
  `~/.local/bin` to your `PATH`.
- **`uv sync --frozen` fails saying the lockfile is out of date**: someone edited
  `pyproject.toml` without committing `uv.lock`. Run `uv lock`, then commit
  `uv.lock`.
- **Port 8000 already in use**: add `--port 8001` to the run command.
- **`/ready` or `/generate` returns 503**: the model server isn't ready yet. Run
  `docker compose ps` and wait until `llm` is `healthy` (the first start has to
  download the model).
