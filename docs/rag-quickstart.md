# RAG: retrieval, generation, and Qwen

```text
Index: catalog.sqlite -> vectordb.loader -> FastEmbed (BGE + BM25) -> Qdrant
Query: /ask -> generation -> vectordb.Searcher -> evidence + question -> Qwen
```

The `ingest/` and `vectordb/` modules provide the team's data contract and retrieval
implementation. `generation/service.py` contains `GenerationSettings` and the
service that builds evidence prompts and calls the existing model client.
`generation/schemas.py` defines the request and answer formats.

## Start the unified stack

All services are defined in `compose.yml`: `app`, `llm` (llama.cpp running
Qwen2.5-1.5B-Instruct), `qdrant`, and one-off `jobs`. The LLM downloads the pinned
GGUF model on first startup; no Ollama installation or `ollama pull` is needed.
FastEmbed handles BGE/BM25 embedding resources separately.

Requirements: Docker, network access for initial downloads, and available disk
space. For Jetstream2, configure the extra disk as described in
[deployment.md](deployment.md#data-volume-first-time-only).

`DATA_VOLUME_DIR` defaults to `./volume`. Ingestion stores its catalog at
`./volume/data/catalog.sqlite`, Qdrant stores its index under `./volume/qdrant`,
and both the API and jobs use `./volume/embed-cache`. Setting `DATA_VOLUME_DIR`
changes the shared root for all three. Generated data is excluded from Git.

An existing development `.env` overrides deployment defaults. Before running
Compose, review it: use `LLM_PROVIDER=openai`,
`LLM_BASE_URL=http://llm:8080/v1`, `LLM_MODEL=qwen2.5-1.5b-instruct`, and
`GENERATION_ENABLED=true` for this stack. Use `APP_ENVIRONMENT=production` and
`APP_DEBUG=false` for deployment. Keep intentional volume paths and credentials.
See the deployment guide if a bind-mount permission error occurs: jobs need write
access to `data` and both app and jobs need write access to `embed-cache`.

```bash
# Start and wait for the model server and vector database.
docker compose up -d --wait llm qdrant

# Create the SQLite schema, fetch regulations, then parse them.
docker compose run --rm --build jobs python -m myapp.ingest.catalog migrate
docker compose run --rm jobs python -m myapp.ingest fetch --part "8 CFR 214"
docker compose run --rm jobs python -m myapp.ingest parse --part "8 CFR 214"

# Build and activate the hybrid retrieval index.
docker compose run --rm jobs python -m myapp.vectordb.loader --activate

# Start the API and check readiness before asking a question.
docker compose up -d --build app
curl -f http://localhost:8000/ready
curl http://localhost:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"What does Form I-983 relate to?","visa_type":"F-1"}'
```

If a current catalog already exists, place a consistent database copy at
`$DATA_VOLUME_DIR/data/catalog.sqlite` (or `./volume/data/catalog.sqlite` when
unset), migrate it, and skip fetch/parse. The loader defaults to `/data/catalog.sqlite`
inside jobs, where `INGEST_DATA_DIR=/data` is explicitly configured.

Jobs run only when requested with `docker compose run`; normal `up` starts app,
llm, and Qdrant. Each normal load builds a new collection and activates its alias
after completion. For an interrupted build, follow the README's
`--version N --replace` procedure.

Older prototype files under `./data` and Docker volumes named `rag-vectors`,
`rag-models`, or `rag-embeddings` are not automatically migrated or deleted.
To reuse an old catalog, copy it consistently to the unified data directory and
rebuild the index there. Single-vector prototype indexes are incompatible with
the team's hybrid search.
If the previous prototype is still running under the same Compose project, use
`docker compose down --remove-orphans` before starting the unified stack. This
stops old services such as Ollama while retaining their stored data; omit `-v`.

## API contract

`POST /ask` and `POST /retrieve` accept the same request:

```json
{
  "question": "What does Form I-983 relate to?",
  "k": 3,
  "visa_type": "F-1",
  "as_of": "2024-06-01",
  "sources": ["ecfr"],
  "include_proposed": false
}
```

Only `question` is required. Omitted filters use the team's retrieval defaults.

- `/retrieve` returns the team's `Hit` list without calling the LLM.
- `/ask` returns `answer` and `sources`; citation `[1]` refers to `sources[0]`.
- Sources include the provision ID, text, parent lead-in, source, authority, status, validity dates,
  `score` (hybrid ranking score), and `relevance` (dense cosine similarity).
- Generation limits evidence text to a character budget. Returned source text
  matches the excerpts sent to the LLM; `/retrieve` returns full retrieved text.
- Empty retrieval returns an insufficient-evidence answer without an LLM call.
- Disabled generation or Qdrant request failures return 503; LLM timeouts return 504.

## Configuration and storage

| Prefix | Purpose |
| --- | --- |
| `INGEST_*` | Ingestion settings |
| `QDRANT_*`, `EMBED_*`, `SEARCH_*` | Team retrieval connection, models, and search settings |
| `GENERATION_ENABLED` | Initialize retrieval and enable question answering at startup |
| `GENERATION_MAX_CONTEXT_CHARS` | Evidence text budget, default 6000 characters |
| `LLM_*` | Existing generation model client settings |

Compose reads `.env.production.example`, then an optional `.env` for overrides.
The app explicitly receives the generation enable flag, context budget, model
connection, Qdrant URL, and `/embed-cache` path. Generation defaults to enabled
in Compose and disabled in direct local Uvicorn runs unless configured otherwise.
`LLM_PROVIDER=openai` selects the compatible request format; the default backend
is the team's llama.cpp container running Qwen.

| Mount or volume | Contents |
| --- | --- |
| `$DATA_VOLUME_DIR/data:/data` | Jobs read/write the raw files and SQLite catalog |
| `$DATA_VOLUME_DIR/qdrant:/qdrant/storage` | Qdrant vectors, text, and metadata |
| `$DATA_VOLUME_DIR/qdrant-snapshots:/qdrant/snapshots` | Qdrant snapshot storage |
| `$DATA_VOLUME_DIR/embed-cache:/embed-cache` | BGE/BM25 cache shared by jobs and API |
| `llm-models:/models` | llama.cpp generation model files, managed by Docker |

The first four mounts use `./volume` if `DATA_VOLUME_DIR` is unset. The model's
named volume uses Docker's storage location; it is not moved by `DATA_VOLUME_DIR`.

The API creates one searcher per process. Synchronous endpoints run in FastAPI's
worker pool. A lock serializes access to shared embedding models; LLM generation
runs outside that lock.

## Readiness and validation

`GET /ready` returns `{"status":"ready"}` only after the configured model appears
in the model server's listing and, when generation is enabled, retrieval checks
pass: the encoder is initialized, the live alias is activated, the index is
nonempty, and its vector configuration and recorded embedding model match the
loaded encoder. One evidence record is checked for a provision ID and text.
The probe never generates answers, runs embeddings, or changes data.

Unavailable dependencies return 503 with a short reason. Model-list timeouts
return 504; malformed model listings return 502. `/health` checks only the API
and stays available during slow or failed readiness probes. Network calls use
their clients' timeouts. If generation is disabled, retrieval is not checked;
if it is enabled, the development echo model is rejected.

```bash
uv sync --extra dev --frozen
uv run pytest
```

Generation integration tests use temporary fixture catalogs, the actual team
loader/searcher, and in-memory Qdrant. Embedding and LLM inference are replaced by
test doubles; tests do not download models or require external services.

The loader currently uses one provision version per chunk, with parent lead-in
context also passed to the generation prompt. The character budget
does not guarantee an exact token limit. `SEARCH_MIN_RELEVANCE` is 0.65, following
the ingestion PR's evaluation; broader evaluation is still needed and retrieved
evidence may be insufficient. Citations are
requested in the prompt but are not automatically verified. Incremental sync and
snapshots remain unimplemented. Readiness checks model availability and index
metadata; they do not prove that inference succeeds or answers are accurate.
Real-data/model smoke tests and Jetstream2 deployment validation remain necessary.
Qwen is the current prototype default;
the team's final generation model is still to be agreed.
