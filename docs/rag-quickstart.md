# RAG: retrieval, generation, and Qwen

```text
Index: catalog.sqlite -> vectordb.loader -> FastEmbed (BGE + BM25) -> Qdrant
Query: /ask -> generation -> vectordb.Searcher -> evidence + question -> Qwen
```

The `ingest/` and `vectordb/` modules provide the team's data contract and retrieval
implementation. `generation/service.py` contains `GenerationSettings` and the
service that builds evidence prompts and calls the existing model client.
`generation/schemas.py` defines the request and answer formats.

## Start the current RAG prototype

Requirements: Docker, available disk space, and a real `catalog.sqlite` generated
by ingestion. Place it at `data/catalog.sqlite`. Generated databases are excluded
from Git; pulling a branch does not download its runtime database.

If the original Compose stack is running, stop it with `docker compose down`
before switching to this stack. Normal `down` preserves named volumes; `down -v`
deletes them.

```bash
# Start the model server and vector database.
docker compose -f compose.rag.yml up -d --wait ollama qdrant

# Download the generation model. FastEmbed handles embedding models separately.
docker compose -f compose.rag.yml exec ollama ollama pull qwen2.5:1.5b-instruct

# Build and activate an index using the team's loader.
docker compose -f compose.rag.yml run --rm --build app \
  python -m myapp.vectordb.loader --catalog /data/catalog.sqlite --activate

# Start the API.
docker compose -f compose.rag.yml up -d --build app

# Ask a question.
curl http://localhost:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"What does Form I-983 relate to?","visa_type":"F-1"}'
```

The first load downloads embedding resources into a shared cache. Each normal
load builds a new collection and activates its alias after loading completes.
For an interrupted build, follow the README's `--version N --replace` procedure.
Older prototype collections with a single unnamed vector are incompatible with
the team's hybrid search and must be replaced by a new catalog import.

Qdrant currently has only a `service_started` dependency in this prototype file.
If the loader encounters a startup connection error, confirm Qdrant is ready and
retry. This file does not configure Jetstream2's additional disk: named volumes
use Docker's storage location, and `./data` belongs to the current checkout.
The team's separate Compose/data-volume work must be integrated before deployment.

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
- Sources include the provision ID, text, source, authority, status, validity dates,
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

RAG Compose reads `.env.production.example` and explicitly overrides the Ollama
address, model, generation flag, and embedding-cache path. It does not pass the
local `.env` into the app. Direct local Uvicorn runs still read `.env`; enable
generation and configure a working LLM and Qdrant there when needed.
`LLM_PROVIDER=openai` selects the compatible request format; this stack uses Ollama.

| Mount or volume | Contents |
| --- | --- |
| `./data:/data:ro` | Read-only catalog access for the loader |
| `rag-vectors` | Qdrant vectors, text, and metadata |
| `rag-models` | Ollama generation model files |
| `rag-embeddings` | BGE/BM25 cache shared by the loader and API |

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

The loader currently uses one provision version per chunk. The character budget
does not guarantee an exact token limit. Retrieval thresholds need evaluation on
real questions; retrieved evidence may still be insufficient. Citations are
requested in the prompt but are not automatically verified. Incremental sync and
snapshots remain unimplemented. Readiness checks model availability and index
metadata; they do not prove that inference succeeds or answers are accurate.
Real-data/model smoke tests and Jetstream2 deployment validation remain necessary.
Qwen is the current prototype default;
the team's final generation model is still to be agreed.
