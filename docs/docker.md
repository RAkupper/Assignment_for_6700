# Docker setup

## Build the image

Run from the repository root:

```bash
docker build -t immigration-app:dev .
```

The Dockerfile uses Python 3.13 and installs dependencies from `uv.lock`
with `uv sync --frozen`. Dependencies are installed before the source is
copied, so source changes can reuse the dependency layer.

The final image contains the installed application and runtime dependencies.
Build tools and development dependencies stay out of the final image.

## Run the container

```bash
docker run -d --name immigration-app-test \
  -p 127.0.0.1:8000:8000 \
  --env-file .env.production.example \
  immigration-app:dev
```

Configuration is supplied at runtime. For private settings, use an
untracked environment file with `--env-file`.

The `.dockerignore` excludes environment files, Git history, virtual
environments, credentials, and local build files.

## Verify the container

```bash
curl http://127.0.0.1:8000/health
docker exec immigration-app-test id
docker exec immigration-app-test python -m assignment_1.healthcheck
docker inspect --format '{{.State.Health.Status}}' immigration-app-test
```

The health endpoint returns:

```json
{"status":"ok","version":"0.1.0"}
```

The container runs as `appuser` with UID 10001. The Python health check
returns exit code 0 when the endpoint returns HTTP 200 and its JSON
contains `"status": "ok"`. Otherwise, it returns exit code 1.

Docker runs this check every 30 seconds, with a 5-second timeout,
a 10-second startup period, and 3 retries.

## Build measurements

Measured locally on Windows with Docker Desktop and WSL2 Ubuntu 22.04.
Docker reported 24 CPUs and 31.2 GiB of memory.

| Measurement | Result |
| --- | --- |
| Image size reported by Docker inspect | 207,877,774 bytes |
| Rebuild with cached dependencies | 2.60 seconds |
| Rebuild with Docker layer caching disabled | 2.75 seconds |

A single comment line was temporarily added to
`src/assignment_1/healthcheck.py`. Both builds used that same changed source.
The first build reused the dependency layer. The second used `--no-cache`
and installed dependencies again. The original source was then restored.

Both builds reused locally downloaded base images. These are single-run
measurements, so the small timing difference does not establish a reliable
performance improvement.

Raw build logs, timings, and image size are saved in `outputs/docker/`.

## Stop and remove the test container

```bash
docker stop immigration-app-test
docker rm immigration-app-test
```

## Compose deployment

The Dockerfile serves the app on port 8000 and provides its Docker health
check. It is designed for the Compose stack in PR #15, tracked by issue #14.

Jetstream2 deployment, external access, restart verification, and instance
disk measurements are covered by the deployment work.