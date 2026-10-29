# Local prototype verification

Verified locally on 2026-10-28 using Docker Compose, Qwen2.5-1.5B-Instruct
through llama.cpp, and the team's BGE/BM25 retrieval pipeline.

The local catalog contains 2,821 provision versions from one eCFR snapshot dated
2026-09-30. This is a smoke dataset, not the full historical corpus.

| Check | Result |
| --- | --- |
| Unit/integration suite | 187 tests passed |
| Ruff and mypy | Passed |
| Compose configuration | Passed |
| Live health/readiness | Passed |
| Live retrieval | Returns evidence including the on-campus employment provision |
| Live browser-to-model request | Returns an answer and supporting passages |
| Citation UI | Valid labels open/focus evidence; missing labels remain optional |
| Mobile layout | No horizontal overflow at 390 px |
| Browser JavaScript errors | None observed |

Run the operational smoke check again with:

```bash
python scripts/smoke_rag.py
```

This checks service connectivity and response structure, not answer correctness.
Initial evaluation exposed an off-campus result ranked ahead of the on-campus
result and model confusion between the two categories. The prompt encourages
topic separation and citations, but citations are not enforced. Answers and
attribution still require review; this prototype does not claim production accuracy.

The UI is served by the existing app container at `/`. No frontend container or
Node.js runtime is required. Local runtime data under `volume/` and `.env` are
gitignored. Jetstream2 deployment has not been verified by this local smoke test.
