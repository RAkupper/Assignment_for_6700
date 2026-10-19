# Assignment_for_6700

An installable FastAPI service skeleton for our course ML inference API. This week it
exposes a typed `/health` endpoint; later assignments will put a model behind it.

## Contents

- [Quickstart](#quickstart) (tutorial: clean machine to running service)
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

In one terminal, run a single server worker (use the same command before and
after the blocking-call fix):

```bash
uv run uvicorn myapp.app:app --host 127.0.0.1 --port 8000 --workers 1
```

In another terminal, send ten concurrent requests:

```bash
uv run python scripts/benchmark_predict.py --output measurements/before.json
```

The script shares one `httpx.AsyncClient` across ten POST requests, each with the
same valid input of 64 zero-valued pixels. `asyncio.gather` schedules them
concurrently and waits for all ten responses. It reports each request's duration,
the batch wall-clock time, and the slowest request. Individual timing starts just
before `client.post` and ends when the response body arrives; batch timing starts
just before `gather` and ends when all ten requests finish. Client creation and
printing/saving results are outside the batch timer. There are no warm-up requests.

After fixing the handler and restarting the server, run the same script with
`--output measurements/after.json`. The client remains asynchronous in both runs;
the server-side blocking call is what changes. A fast model may hide the effect:
if adding `time.sleep` to simulate a slow call, retain the same delay in both
versions and document it. Request durations include queueing and network overhead,
so they do not isolate model execution time.

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


### Endpoints

| Method | Path      | Response model   | Description                              |
| ------ | --------- | ---------------- | ---------------------------------------- |
| GET    | `/health` | `HealthResponse` | Liveness check with service version     |
| GET    | `/docs`   | HTML             | Auto-generated Swagger UI (FastAPI)      |

`HealthResponse` fields:

| Field     | Type          | Example   |
| --------- | ------------- | --------- |
| `status`  | `"ok"`        | `"ok"`    |
| `version` | `str`         | `"0.1.0"` |

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
├── src/myapp/
│   ├── __init__.py
│   ├── app.py                 # FastAPI app, Pydantic models, routes
│   ├── config.py              # typed Settings read from the environment
│   └── main.py                # CLI entry-point placeholder
└── tests/
    ├── test_config.py         # Settings validation tests
    └── test_health.py         # /health endpoint tests
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
