# Model serving: architecture decision

Status: accepted. Builds on #5 / PR #6 (model client interface). Deployed on
TEAM02 in #15 (compose stack); measurements from that deployment are below.

## Decision

1. **The model runs in its own container**, an OpenAI-compatible server
   (the llama.cpp server with Qwen2.5-1.5B-Instruct Q4_K_M by default, vLLM if we
   get a GPU instance). The app talks to it over HTTP through
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
The quantized llama.cpp numbers from the real deployment are in section 4.

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

### 4. Why llama.cpp and not Ollama

Measured on TEAM02 (Jetstream2 m3.small: 2 vCPUs, 6 GB RAM, 20 GB root disk),
see `docs/deployment.md` from #15:

| | Ollama | llama.cpp server (chosen) |
| --- | --- | --- |
| Server image | 3.78 GB | **309 MB** |
| Fits on the 20 GB disk next to a model | no | yes, 4.4 GB left free with the stack up |

With llama.cpp serving Qwen2.5-1.5B-Instruct (Q4_K_M):

- Weights: 1.1 GB in the `llm-models` volume (`docker system df` shows 1.117 GB).
- Model server healthy 6 s after start with weights present, 8 s after a Docker restart.
- Generation: about 25 tokens/s for one request.
- 10 concurrent requests of up to 64 tokens: **23 s total, slowest 22.9 s**.
- Full stack from a fresh clone (`docker compose up -d --build`): healthy in 14 s;
  from an empty volume, including the model download, 13 s to a healthy app.
- App image: 208 MB. Port 8080 is not reachable from outside; `/health` answers
  from a laptop in 0.06 s.

At 25 tokens/s, `LLM_MAX_TOKENS` is capped at 128 in compose so answers under
concurrent load stay inside the 120 s client timeout.

## Configuration reference

| Variable | Default | Used by | Meaning |
| --- | --- | --- | --- |
| `LLM_PROVIDER` | `echo` | app | `echo` (no server; tests and CI) or `openai` (any OpenAI-compatible server). Must not be `echo` when `APP_ENVIRONMENT=production`. |
| `LLM_BASE_URL` | `http://localhost:11434/v1` | app | Server root including `/v1`. In compose: `http://llm:8080/v1`. |
| `LLM_MODEL` | `qwen2.5:1.5b-instruct` | app, model server | Model name as the server knows it. Compose sets `qwen2.5-1.5b-instruct`, the `--alias` given to llama.cpp. |
| `LLM_HF_REPO` | `Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M` | model server | GGUF repo and quant that llama.cpp downloads with `-hf`. |
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

**E (compose / Jetstream2).** Implemented in `compose.yml` (#15). The model
server is the upstream llama.cpp image, pinned by digest:

```yaml
services:
  llm:
    image: ghcr.io/ggml-org/llama.cpp:server@sha256:<pinned-digest>
    command: ["-hf", "${LLM_HF_REPO:-Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M}",
              "--alias", "${LLM_MODEL:-qwen2.5-1.5b-instruct}", "-c", "4096"]
    environment:
      LLAMA_CACHE: /models
    volumes:
      - llm-models:/models
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      start_period: 120s
    restart: unless-stopped

  app:
    build: .
    environment:
      LLM_PROVIDER: openai
      LLM_BASE_URL: http://llm:8080/v1
    depends_on:
      llm: { condition: service_healthy }
    restart: unless-stopped

volumes:
  llm-models:
```

llama.cpp downloads the weights itself on first start and reuses them from the
volume afterwards, so no separate pull service is needed, and restarts and
unshelves don't download them again.

## Still to measure on Jetstream2

Latency, concurrency and disk use are measured (section 4). What remains:

- answer quality of Qwen2.5-1.5B (Q4_K_M) on a handful of our test questions,
  using `scripts/bench_model.py` against the running stack;
- if quality falls short, the same run with a larger GGUF via `LLM_HF_REPO`,
  checking it still fits on the 20 GB disk.

The default model is the smallest one that fits the instance; we move up only if
its answers don't hold up.
