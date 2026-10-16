# Assignment_for_6700

An installable FastAPI service skeleton for our course ML inference API. This week it
exposes a typed `/health` endpoint; later assignments will put a model behind it.

## Contents

- [Quickstart](#quickstart) (tutorial: clean machine to running service)
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
uv run uvicorn assignment_1.app:app --reload
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

## How-to guides

### Run the tests

```bash
uv run pytest
```

With a coverage report:

```bash
uv run pytest --cov=assignment_1
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

`Settings` in `src/assignment_1/config.py` reads environment variables and the
`.env` file in the working directory. Environment variables take precedence over
`.env`; missing values use the defaults below.

| Variable | Default | Meaning and validation |
| -------- | ------- | ---------------------- |
| `APP_NAME` | `assignment-for-6700` | Application title; must not be empty or whitespace-only. |
| `APP_ENVIRONMENT` | `development` | Must be `development` or `production`. |
| `APP_DEBUG` | `false` | Boolean debug flag; must be `false` in production. |

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

Model failures reach clients as `504` (timeout), `503` (server unreachable or
erroring) or `502` (malformed reply). See
[docs/model-serving.md](docs/model-serving.md) for where the model runs and why.

### Run against a local model

```bash
ollama pull qwen2.5:1.5b-instruct     # once; needs https://ollama.com installed
LLM_PROVIDER=openai uv run uvicorn assignment_1.app:app --reload
LLM_PROVIDER=openai uv run python scripts/bench_model.py   # latency check
```


### Endpoints

| Method | Path      | Response model   | Description                              |
| ------ | --------- | ---------------- | ---------------------------------------- |
| GET    | `/health` | `HealthResponse` | Liveness check with service version     |
| POST   | `/generate` | `GenerateResponse` | Model completion for a prompt |
| GET    | `/docs`   | HTML             | Auto-generated Swagger UI (FastAPI)      |

`HealthResponse` fields:

| Field     | Type          | Example   |
| --------- | ------------- | --------- |
| `status`  | `"ok"`        | `"ok"`    |
| `version` | `str`         | `"0.1.0"` |

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

### Project layout

```text
Assignment_for_6700/
├── .github/
│   ├── ISSUE_TEMPLATE/        # bug report, documentation, feature request
│   └── workflows/ci.yml       # GitHub Actions quality gates
├── .env.local.example         # local development settings
├── .env.production.example    # deployed settings
├── .pre-commit-config.yaml    # Git hooks (ruff, hygiene checks)
├── .python-version            # Python 3.13, read by uv and CI
├── pyproject.toml             # metadata, dependencies, tool config
├── uv.lock                    # pinned, shared environment
├── src/assignment_1/
│   ├── __init__.py
│   ├── app.py                 # FastAPI app, Pydantic models, routes
│   ├── config.py              # typed Settings and ModelSettings from the environment
│   ├── model_client.py        # ModelClient protocol, echo and OpenAI-compatible clients
│   └── main.py                # CLI entry-point placeholder
├── docs/model-serving.md      # model architecture decision and measurements
├── scripts/bench_model.py     # latency benchmark through the service's client
└── tests/
    ├── conftest.py, fakes.py  # FakeModelClient and its fast and slow fixtures
    ├── test_config.py         # Settings validation tests
    ├── test_generate.py       # /generate endpoint, health during a slow request
    ├── test_health.py         # /health endpoint tests
    ├── test_model_client.py   # client lifecycle and dependency override
    └── test_openai_client.py  # model settings, HTTP client, error mapping
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
