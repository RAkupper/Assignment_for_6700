# Model serving: architecture decision

Status: proposed (Part D, P4). Builds on #5 / PR #6 (model client interface).
Needs sign-off from B (inference endpoint), C (Dockerfile) and E (compose, Jetstream2).

## Decision

1. **The model runs in its own container**, an OpenAI-compatible server (Ollama by
   default, vLLM if we get a GPU instance). The app talks to it over HTTP through
   `OpenAICompatibleClient`. A hosted OpenAI-compatible API is the fallback, and
   switching to it is a change of environment variables only.
2. **Model weights are not baked into any image.** The model server downloads them
   once into a **named volume** mounted at the model cache, where they survive
   container rebuilds, restarts and instance unshelves.
3. **Everything is configured through `LLM_*` environment variables** read by
   `ModelSettings` (Pydantic Settings). No URL, model name, device, path or
   credential is hard-coded.

## Options considered

| | A. In the app process | B. Separate model container (chosen) | C. Hosted API |
| --- | --- | --- | --- |
| App dependencies | torch + transformers | HTTP client only | HTTP client only |
| App image | large, rebuilt often | small, rebuilt often | small |
| Memory per app worker | full model copy | none | none |
| Swap or upgrade model | rebuild app | change `LLM_MODEL` | change `LLM_MODEL` |
| Cost | instance only | instance only | per token, needs a key |
| Data leaves our server | no | no | yes |
| Works offline after first pull | yes | yes | no |

## Measurements

Taken on a 2 vCPU Intel Xeon (2.1 GHz), 7 GB RAM Linux machine, Python 3.13,
torch 2.14.1, transformers 5.19.0. Each timing is the median of three runs. The
model numbers use a model with the exact Qwen2.5-0.5B-Instruct architecture
(494 M parameters, bf16) and random weights. File size, load time and memory
depend only on the architecture and dtype, so they match the real checkpoint.
Answer quality and Ollama's quantized speed still need to be measured on
Jetstream2 (see "Still to measure").

### 1. Putting the model in the app process makes the app 400x heavier

| | App only (options B, C) | App + torch + transformers (option A) |
| --- | --- | --- |
| Installed packages (site-packages) | **13 MB** | **5,443 MB** |
| `uv pip install`, cold cache | 0.6 s | 93.5 s |
| Process start (import the app) | 0.31 s | 0.31 s + 1.97 s (import torch/transformers) |
| Load 0.5B weights at startup | n/a | 1.61 s |
| Peak memory per process | **44 MB** | **1,664 MB** |

On Linux, `torch` from PyPI pulls in the CUDA stack (15 `nvidia-*` packages plus
`triton`) even on a CPU-only machine, which is most of the 5.4 GB. Under option
A, every uvicorn worker holds its own copy of the model, so 4 workers would need
about 6.6 GB for even the smallest model. Every code change would also rebuild
or re-push an image layer holding that 5.4 GB.

### 2. The extra network hop is negligible

`OpenAICompatibleClient` against a local stand-in OpenAI-compatible server that
answers instantly (500 sequential calls, three runs):

| Median | p95 | Max |
| --- | --- | --- |
| 1.05 to 1.12 ms | 1.33 to 1.45 ms | under 3.5 ms |

For comparison, generating just 32 tokens in-process with the 0.5B model took
3.3 to 4.4 s on the same machine. The HTTP hop adds about 0.03% to a request.

With the stand-in server set to take 2 s per answer, **10 concurrent calls
through one shared client finished in 2.02 s total (slowest 2.00 s)**. The client
pools connections and is safe to share across threads, which is what B needs
for the `asyncio.to_thread` version of the endpoint.

### 3. Weights belong in a volume, not the image

The 0.5B checkpoint is **943 MB** on disk (safetensors, bf16). Baking it in:

- adds 943 MB to the image (about 3 GB for 1.5B, about 6 GB for 3B in bf16);
- re-pushes and re-pulls those layers whenever the base layer or model changes;
- ties a model swap to an image rebuild.

With a named volume, the weights download once per instance, survive
`docker compose down` / `up`, rebuilds and shelve/unshelve, and changing models
is `LLM_MODEL=... docker compose up -d`. The cost is a one-time download on a
fresh instance. That download is a deployment step (below), not a build step.

## Configuration reference

| Variable | Default | Used by | Meaning |
| --- | --- | --- | --- |
| `LLM_PROVIDER` | `echo` | app | `echo` (no server; tests and CI) or `openai` (any OpenAI-compatible server). Must not be `echo` when `APP_ENVIRONMENT=production`. |
| `LLM_BASE_URL` | `http://localhost:11434/v1` | app | Server root including `/v1`. In compose: `http://ollama:11434/v1`. |
| `LLM_MODEL` | `qwen2.5:1.5b-instruct` | app, model server | Model name as the server knows it. |
| `LLM_API_KEY` | unset | app | Bearer token; only for hosted APIs. Stored as `SecretStr`, never printed. |
| `LLM_TIMEOUT_S` | `120` | app | Read timeout for one generation; on expiry the API returns 504. |
| `LLM_CONNECT_TIMEOUT_S` | `5` | app | Connect timeout; on failure the API returns 503. |
| `LLM_MAX_TOKENS` | `512` | app | Upper bound on answer length. |
| `LLM_TEMPERATURE` | `0.0` | app | 0 keeps answers deterministic, which helps evaluation. |
| `LLM_DEVICE` | `cpu` | model server | `cpu`, `cuda` or `auto`; compose uses it to decide on a GPU reservation. |
| `LLM_CACHE_DIR` | `/models` | model server | Mount point of the weights volume. |

Error mapping (in `app.py`): timeout → **504**, server unreachable or HTTP error →
**503**, malformed response → **502**. The body is `{"detail": "..."}` and never
contains the prompt or the API key.

## Hand-offs

**B (async inference).** Depend on the client with
`Annotated[ModelClient, Depends(get_model_client)]`. `generate()` is synchronous,
so call it via `await asyncio.to_thread(model.generate, prompt)` or from a plain
`def` route. Calling it directly inside an `async def` route is the "deliberately
blocking" version. The client is built once in the lifespan handler (PR #6).

**C (Dockerfile).** The app image needs no torch, transformers or weights. Keep
the model server's image out of our Dockerfile; we use the upstream image.

**E (compose / Jetstream2).** Suggested services, to adapt in `compose.yml`
(pin a specific `ollama/ollama` tag rather than `latest`):

```yaml
services:
  ollama:
    image: ollama/ollama:<pinned-tag>
    environment:
      OLLAMA_MODELS: ${LLM_CACHE_DIR:-/models}
    volumes:
      - ollama-models:${LLM_CACHE_DIR:-/models}
    healthcheck:
      test: ["CMD", "ollama", "list"]
      interval: 10s
      retries: 12
    restart: unless-stopped

  model-pull:            # one-shot: downloads weights into the volume if missing
    image: ollama/ollama:<pinned-tag>
    environment:
      OLLAMA_HOST: ollama:11434
    entrypoint: ["ollama", "pull", "${LLM_MODEL}"]
    depends_on:
      ollama: { condition: service_healthy }
    restart: "no"

  app:
    build: .
    env_file: .env
    environment:
      LLM_PROVIDER: openai
      LLM_BASE_URL: http://ollama:11434/v1
    depends_on:
      ollama: { condition: service_healthy }
      model-pull: { condition: service_completed_successfully }
    restart: unless-stopped

volumes:
  ollama-models:
```

`ollama pull` is a no-op when the weights are already in the volume, so restarts
and unshelves don't download them again.

## Still to measure on Jetstream2

Run `scripts/bench_model.py` on the instance with the real model (it uses the same
client and env vars as the app) for 2 or 3 candidate models, for example
`qwen2.5:1.5b-instruct` and `llama3.2:3b`, and record:

- first-call latency (server-side model load) and sequential median latency;
- total and slowest time for 10 concurrent requests;
- answer quality on a handful of our test questions;
- `docker system df` with the weights volume present.

We pick the smallest model whose answers hold up on the test questions. The
default in `ModelSettings` is a starting point for that comparison, not a final
choice.
