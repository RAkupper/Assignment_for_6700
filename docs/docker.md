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
docker exec immigration-app-test python -m myapp.healthcheck
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

### Final image, on TEAM02

Measured on our Jetstream2 instance (m3.small: 2 vCPUs, 6 GB RAM) with the final
`main`, which adds scikit-learn, numpy and scipy for `/predict`.

| Measurement | Result |
| --- | --- |
| Image size (`docker image ls`) | 472 MB |
| Rebuild after a one-line change, dependency layer cached | 13.1 seconds |
| Rebuild after a one-line change, `--no-cache` | 15.6 seconds |

A comment line was appended to `src/myapp/main.py`, the image was rebuilt with
`docker compose build app` and then with `docker compose build --no-cache app`, and
the change was reverted. The gap is small because uv installs the 25 packages in
under 0.1 s; most of each build is copying the roughly 400 MB virtual environment
into the runtime stage and exporting the image, which happens either way.

`docker system df` with the stack running is in
[deployment.md](deployment.md#measured-on-team02).

### Earlier image, before scikit-learn

Measured locally on Windows with Docker Desktop and WSL2 Ubuntu 22.04.
Docker reported 24 CPUs and 31.2 GiB of memory.

| Measurement | Result |
| --- | --- |
| Image size reported by Docker inspect | 207,877,774 bytes |
| Rebuild with cached dependencies | 2.60 seconds |
| Rebuild with Docker layer caching disabled | 2.75 seconds |

A single comment line was temporarily added to
`src/myapp/healthcheck.py`. Both builds used that same changed source.
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
check. `compose.yml` builds it and runs it next to the llama.cpp model server;
see [deployment.md](deployment.md) for the Jetstream2 deployment, external access,
restart checks and disk usage.
