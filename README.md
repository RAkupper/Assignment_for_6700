# Assignment_for_6700

An installable FastAPI service skeleton for our course ML inference API. This week it
exposes a typed `/health` endpoint; later assignments will put a model behind it.

Update for HW2: it now has two models, a scikit-learn digit classifier inside the app
(`/predict`) and Qwen2.5-1.5B-Instruct running in a llama.cpp container
(`/generate`). We deploy it on Jetstream2 with `docker compose up -d`.

Final project: the retrieval layer for RAG over U.S. immigration rules is in progress.
Provision text from the ingestion catalog is embedded and stored in a Qdrant vector
database with dense (BGE) and sparse (BM25) vectors and filterable metadata. See
[Vector database](#vector-database-qdrant).

## Contents

- [Quickstart](#quickstart) (tutorial: clean machine to running service)
- [Deploy with Compose](#deploy-with-compose) (run everything on an instance)
- [Docker](#docker) (build, run, and check the container)
- [Vector database (Qdrant)](#vector-database-qdrant) (collection, loading, rollback)
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

This is how we run it on our Jetstream2 instance (TEAM02). It should work on any Linux
machine with Docker. More notes in [docs/deployment.md](docs/deployment.md).

### 1. Requirements

- Docker and Docker Compose 2.24 or newer:

  ```bash
  docker compose version
  ```

- Port 8000 open (Exosphere instances already have this)
- About 4 GB of free disk

### 2. Start it

For a fresh clone with no local `.env` file:

```bash
git clone https://github.com/RAkupper/Assignment_for_6700.git
cd Assignment_for_6700
docker compose up -d --build
```

**If you already followed Quickstart:** reuse your existing checkout and stop its
Uvicorn server first. The Quickstart `.env` overrides Compose's deployment defaults
and selects `LLM_PROVIDER=echo`, so `/generate` would echo the prompt rather than
call the model container. Before starting Compose, temporarily move that file aside
(choose another backup name if this one already exists):

```bash
mv .env .env.local-backup
docker compose up -d --build
```

The first run downloads the model (1.1 GB), so it takes a bit longer. Deployment
defaults are in `.env.production.example` and `compose.yml`; use a `.env` file for
intentional overrides.

Wait until both containers show `healthy`:

```bash
docker compose ps
```

### 3. Test the endpoints

On the instance use localhost. From your laptop use the instance IP instead (ours is
`149.165.150.147`).

```bash
HOST=http://localhost:8000
```

`/health`:

```bash
curl $HOST/health
```

Should return:

```json
{"status":"ok","version":"0.1.0"}
```

`/ready` (gives 503 if the model server isn't up yet):

```bash
curl $HOST/ready
```

Should return:

```json
{"status":"ready"}
```

`/predict` takes the 64 pixel values (0-16) of an 8x8 image. This one is a 0. It takes
about 1 s because of the `time.sleep(1)` we kept from the audit
([discussion.md](discussion.md)).

```bash
curl -X POST $HOST/predict \
  -H "Content-Type: application/json" \
  -d '{"pixels":[0,0,5,13,9,1,0,0,0,0,13,15,10,15,5,0,0,3,15,2,0,11,8,0,0,4,12,0,0,8,8,0,0,5,8,0,0,9,8,0,0,4,11,0,1,12,7,0,0,2,14,5,10,12,0,0,0,0,6,13,10,0,0,0]}'
```

Should return:

```json
{"digit":0}
```

`/generate`:

```bash
curl -X POST $HOST/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "In one sentence, what is OPT for F-1 students?"}'
```

Should return:

```json
{"completion":"OPT stands for Optional Practical Training, which allows F-1 students to gain practical work experience in their field of study."}
```

Bad input returns 422:

```bash
curl -i -X POST $HOST/predict \
  -H "Content-Type: application/json" \
  -d '{"pixels":[1,2,3]}'
```

Swagger docs are at `$HOST/docs`.

### 4. Other commands

```bash
docker compose logs -f app
docker compose down
docker compose up -d
```

`down` keeps the downloaded model. The containers restart by themselves after a reboot
or unshelve (`restart: unless-stopped`).

If you backed up the Quickstart `.env`, restore it when returning to local
development (after stopping Compose):

```bash
docker compose down
mv .env.local-backup .env
```

### 5. Using a different model server

Set `LLM_BASE_URL` and `LLM_MODEL` in `.env`. For example, for the class server:

```bash
LLM_BASE_URL=http://10.1.82.181:8000/v1
LLM_MODEL=Qwen/Qwen2.5-14B-Instruct-AWQ
```

Then run `docker compose up -d` again. See [docs/model-serving.md](docs/model-serving.md).

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

Note: this only starts the app. `/generate` and `/ready` need the model server too, so
use [Compose](#deploy-with-compose) for the full thing.

## Vector database (Qdrant)

Retrieval reads from a Qdrant collection built from `data/catalog.sqlite` (see
[docs/ingestion.md](docs/ingestion.md) for the catalog and the handoff contract).
Compose integration is not done yet, so for now Qdrant runs as its own container.

### 1. Start Qdrant locally

```bash
docker run -d --name qdrant --restart unless-stopped -p 6333:6333 \
  -v qdrant_data:/qdrant/storage qdrant/qdrant:v1.19.2
```

The image is pinned so the server stays within one minor version of
`qdrant-client`. Data lives in the `qdrant_data` volume and survives
`docker rm`. The dashboard is at <http://localhost:6333/dashboard>.

### 2. Load the catalog

Loading is a one-off task, never part of app startup, so two app copies can never
load at once:

```bash
uv run python -m myapp.vectordb.loader --catalog data/catalog.sqlite --activate
```

This builds the next free version (`provisions_v1`, then `provisions_v2`, ...)
beside the live one, embeds every provision version from succeeded ingestion runs,
and with `--activate` moves the `provisions` alias to it in one atomic step. It never
writes into the live collection. If a load crashes partway, rerun it with
`--version N --replace` to rebuild that unfinished version. The first run downloads
the embedding models (about 70 MB).

### 3. Check and roll back

```bash
uv run python -m myapp.vectordb.collection status
```

```text
alias provisions -> provisions_v2
provisions_v1: 1520 points
provisions_v2: 1534 points  (live)
```

To roll back, point the alias at the previous version. Nothing is reloaded:

```bash
uv run python -m myapp.vectordb.collection activate provisions_v1
```

Delete an old collection only after the new one has been live for a release.

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

The saved model is intended to be loaded once at API startup when `/predict` is
implemented; training does not run as part of service startup or a request.
Keep `models/digits.joblib` with the project and include it in the Docker image.

### Measure concurrent prediction requests

Run these commands from the project root. Start on your working branch with
local changes committed. Both `blocking-before` and `blocking-after` must be
available locally; if needed, download the tags with `git fetch origin --tags`.

Stop any server using port 8000 first. These commands work without a `.env` file;
the audit versions use their default settings.

**If you already followed Quickstart:** its `.env` contains `LLM_*` settings that
neither audit tag accepts, causing `Extra inputs are not permitted` at startup.
Git leaves this ignored file in place when switching tags. Before continuing,
temporarily move it aside (choose another backup name if this one already exists).
Do the same for any other existing `.env` containing settings unsupported by the
audit tags:

```bash
mv .env .env.audit-backup
```

In terminal 1, remember your branch, switch to the blocking version, install its
dependencies, and start one server worker. Keep this terminal open until you
return to your branch:

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
and resync its dependencies in that same terminal. Do this even if a benchmark fails:

```bash
git switch "$audit_branch"
uv sync --extra dev --frozen
```

If you moved `.env` aside, restore it after returning to your branch (use the
backup name you chose):

```bash
mv .env.audit-backup .env
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

Those are the code defaults. Under Compose they're set for the llama.cpp container
(`LLM_PROVIDER=openai`, `LLM_BASE_URL=http://llm:8080/v1`,
`LLM_MODEL=qwen2.5-1.5b-instruct`, `LLM_MAX_TOKENS=128`).

Model failures reach clients as `504` (timeout), `503` (server unreachable or
erroring) or `502` (malformed reply). See
[docs/model-serving.md](docs/model-serving.md) for where the model runs and why.

### Vector database settings

`QdrantSettings` in `src/myapp/vectordb/collection.py` reads the `QDRANT_` prefix.

| Variable | Default | Meaning and validation |
| -------- | ------- | ---------------------- |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant server; must be a valid URL. Under Compose this will be `http://qdrant:6333`. |
| `QDRANT_API_KEY` | unset | Kept secret in logs and reprs. |
| `QDRANT_ALIAS` | `provisions` | The name the app queries; versioned collections are `<alias>_v<N>`. |

`EmbeddingSettings` in `src/myapp/vectordb/encoders.py` reads the `EMBED_` prefix.
Changing the model is a setting change plus a rebuild into a new collection version.

| Variable | Default | Meaning and validation |
| -------- | ------- | ---------------------- |
| `EMBED_DENSE_MODEL` | `BAAI/bge-small-en-v1.5` | Dense model (fastembed, ONNX, no PyTorch). The collection's vector size is read from the model. |
| `EMBED_SPARSE_MODEL` | `Qdrant/bm25` | Sparse keyword model; Qdrant applies IDF at query time. |
| `EMBED_CACHE_DIR` | unset | Where model weights are cached; a volume under Docker. |
| `EMBED_BATCH_SIZE` | `64` | Must be positive. |

The Qdrant payload indexes follow the handoff contract in `docs/ingestion.md`:
`provision_id`, `source`, `authority`, `status`, `visa_types` (keyword) and
`valid_from`, `valid_to` (datetime). "In force now" is the filter `valid_to` is
null.

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

`POST /predict` takes a `PredictRequest` and returns a `PredictResponse`. The
prediction runs in a worker thread so the 1 s delay doesn't block other requests.

| Model | Field | Type | Validation or example |
| --- | --- | --- | --- |
| `PredictRequest` | `pixels` | `list[int]` | Exactly 64 integers from 0 to 16, row by row from an 8×8 image, otherwise `422` |
| `PredictResponse` | `digit` | `int` | 0 to 9, e.g. `0` |

Example curl is in [Deploy with Compose](#3-test-the-endpoints).

### Project layout

```text
Assignment_for_6700/
├── .github/
│   ├── ISSUE_TEMPLATE/        # bug report, documentation, feature request
│   └── workflows/ci.yml       # GitHub Actions quality gates
├── .env.local.example         # local development settings
├── .env.production.example    # deployed settings
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
│   ├── main.py                # CLI entry-point placeholder
│   └── vectordb/
│       ├── collection.py      # QdrantSettings, versions, alias swap, status and activate CLI
│       ├── encoders.py        # EmbeddingSettings, dense BGE and sparse BM25 encoders
│       └── loader.py          # one-off task: catalog.sqlite into the next collection version
├── models/digits.joblib       # trained digits classifier loaded at startup
├── docs/model-serving.md      # model architecture decision and measurements
├── docs/deployment.md         # running the stack on Jetstream2, measurements
├── docs/docker.md             # building and checking the app image
├── measurements/              # before/after results of the blocking-call audit
├── outputs/docker/            # raw Docker build logs and timings
├── scripts/bench_model.py     # latency benchmark through the service's client
├── scripts/benchmark_predict.py  # ten concurrent /predict requests for the audit
├── scripts/train_digits.py    # trains and saves models/digits.joblib
└── tests/
    ├── conftest.py, fakes.py  # FakeModelClient and its fast and slow fixtures
    ├── test_config.py         # Settings validation tests
    ├── test_generate.py       # /generate endpoint, health during a slow request
    ├── test_health.py         # /health endpoint tests
    ├── test_healthcheck.py    # container health check exit codes
    ├── test_model_client.py   # client lifecycle and dependency override
    ├── test_openai_client.py  # model settings, HTTP client, error mapping
    ├── test_ready.py          # /ready endpoint and the client ping
    └── vectordb/
        ├── catalog_schema.sql # minimal catalog tables the loader reads
        └── test_loader.py     # loader against a fixture catalog and in-memory Qdrant
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
  and, later, our Jetstream2 deployment.
- **Language model in its own container.** The weights (1.1 GB) stay in a volume
  instead of the app image, and we can switch models with env vars without
  rebuilding.
- **Versioned collections behind an alias.** A rebuild (new embedding model, new
  chunking) goes into `provisions_v<N+1>` beside the live one, and the alias moves in
  one step. Rolling back the app image does not roll back vectors, so a rollback
  moves the alias back instead. Old collections are deleted last.
- **Dense and sparse vectors in one collection.** Exact terms like "I-983" or
  "cap-gap" need keyword matching, and keeping both in Qdrant means the same
  metadata filters apply to both before fusion.
- **The catalog is the source of truth.** Qdrant is derived from `catalog.sqlite`
  and can be rebuilt from it at any time. Point ids are UUIDv5 of provision, version
  and chunk, so reloading updates in place instead of duplicating.
- **Quality gates in two places.** pre-commit stops problems before they are
  committed; CI enforces the same checks for everyone, so the main branch stays
  green.

## Troubleshooting

- **`uv: command not found`**: restart your terminal after installing uv, or add
  `~/.local/bin` to your `PATH`.
- **`uv sync --frozen` fails saying the lockfile is out of date**: someone edited
  `pyproject.toml` without committing `uv.lock`. Run `uv lock`, then commit
  `uv.lock`.
- **`ModuleNotFoundError: No module named 'myapp'` on macOS** even though it worked
  before: Python 3.13 skips `.pth` files that macOS flags as hidden, which happens to
  projects inside an iCloud-synced Desktop or Documents folder. Check with
  `ls -lO .venv/lib/python3.13/site-packages/*.pth`. Quick fix:
  `chflags nohidden .venv/lib/python3.13/site-packages/*.pth`. Lasting fix: keep the
  clone outside the synced folders, then `rm -rf .venv && uv sync --extra dev`.
- **Port 8000 already in use**: add `--port 8001` to the run command.
- **`/ready` or `/generate` gives 503**: the model server isn't ready yet. Check
  `docker compose ps` (the first start has to download the model).
