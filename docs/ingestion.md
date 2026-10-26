# Ingestion

How regulations and guidance get from federal websites into a form the rest of the
app can trust: retrieval, keyword search, change monitoring, and evaluation.

Status: design. Decisions agreed so far are marked **Decided**; open questions are at
the end.

## What the sources actually look like

Checked against live responses on 2026-10-25.

| Source | What we pull | Access | What we found |
|---|---|---|---|
| eCFR | 8 CFR 214, 8 CFR 274a, 22 CFR 41, 22 CFR 62 | Versioner API, XML | Requires `Accept-Encoding: gzip`. 8 CFR 214 is ~150k words, 115k of them in §214.2. Paragraphs are flat `<P>` elements; hierarchy exists only as text markers like `(ii) ...—(A)`, up to six levels deep. `(i)` can be a letter or a roman numeral. Dated versions back to 2017. |
| Federal Register | Documents touching those parts | API JSON for metadata; GovInfo for full text | 68 documents on 8 CFR 214 since 2020, including **proposed** rules (OPT fees, 60-day grace period). Rules run ~150 pages, mostly preamble. Full-text links on federalregister.gov return a bot-check page **with status 200**; GovInfo serves the same text. CFR part filter takes integers only (274a must be queried as 274). |
| USCIS Policy Manual | Vol. 2 Part F (students) and related chapters | HTML | Chapters ~9k words, lettered and numbered headings, an update date, dozens of footnotes citing 8 CFR paragraphs. Returns 403 from our cloud test environment; must be checked from Jetstream2. Current version only, no history API. |
| SEVP (Study in the States) | Student guidance pages | HTML | Refused automated access in testing. Most fragile source. |

## Principles

1. **Never lose what the server sent.** Raw responses are stored unmodified. Every cleaned
   record traces back to the exact response it came from.
2. **The citable unit is the provision, not the page.** A provision is the smallest piece
   someone would cite: `8 CFR 214.2(f)(10)(ii)(A)`, a Policy Manual subsection, a Federal
   Register section. Chunking is a separate, later decision made by retrieval.
3. **Time is part of the data.** Every provision version says when it was in force.
   "What changed" and "what was the rule on date X" are queries, not reconstructions.
4. **Say how authoritative something is.** A regulation, an agency's interpretation, a
   web page, and a proposed rule are not equal, and an answer must not treat them as equal.
5. **Each layer can be rebuilt from the one before.** Changing the cleaning logic means
   re-parsing raw files, not re-downloading.
6. **Logs hold counts, never content.**

## Layers

```
 federal sites ──fetch──▶ raw/            immutable responses + fetch log
                           │ parse (pure, deterministic)
                           ▼
                         catalog.sqlite   provisions, versions, references, changes, runs
                           │ chunk + embed (retrieval team)
                           ▼
                         Qdrant           vectors; payload = provision metadata
```

All of it lives under `INGEST_DATA_DIR`, bind-mounted at `/data` on the instance and
ignored by git and Docker.

```
data/
  raw/<source>/<yyyy-mm-dd>/<sha256>.<ext>.gz   content-addressed, so re-fetching the same bytes stores nothing new
  catalog.sqlite
```

**Decided:** SQLite for the catalog. One file, no server, transactions, and its FTS5
extension gives BM25 keyword search over current provision text for free.

## Catalog tables

| Table | One row per | Key columns |
|---|---|---|
| `runs` | ingestion run | `run_id`, `source`, `started_at`, `finished_at`, `status`, counts |
| `fetches` | HTTP response | `fetch_id`, `run_id`, `url`, `status_code`, `content_type`, `sha256`, `bytes`, `source_version`, `raw_path`, `accepted`, `reject_reason` |
| `provisions` | citable unit (identity only, never changes) | `provision_id` (the citation), `source`, `parent_id`, `ordinal`, `heading`, `heading_path`, `authority`, `visa_types`, `visa_types_rule` |
| `provision_versions` | distinct text of a provision over time | `provision_id`, `version`, `text`, `content_hash`, `valid_from`, `valid_to`, `status`, `source_document`, `fetch_id` |
| `references` | citation found inside a provision | `from_provision_id`, `from_version`, `to_citation`, `to_provision_id` (null if not in our corpus) |
| `changes` | detected difference between runs | `run_id`, `provision_id`, `kind` (added, modified, removed), `old_hash`, `new_hash` |
| `provisions_fts` | FTS5 index over current text | |

A new `provision_versions` row is written only when `content_hash` changes, so
backfilling history costs storage only for paragraphs that actually changed.

### Authority

| Value | Meaning | Sources |
|---|---|---|
| `regulation` | Binding law | eCFR |
| `agency_policy` | Agency's official interpretation | USCIS Policy Manual |
| `guidance` | Informal explanation | SEVP pages |
| `rulemaking` | Explanation of why a rule was made or proposed | Federal Register preambles |

### Status

| Value | Meaning |
|---|---|
| `in_force` | Current law or guidance |
| `pending` | Published, effective date in the future |
| `proposed` | Proposed rule, not law. **Decided:** included, always labeled |
| `superseded` | Replaced; `valid_to` is set |
| `withdrawn` | Proposal withdrawn or rule vacated |

### Visa types

Assigned by rules on the citation, never guessed by a model, and the rule used is stored
in `visa_types_rule` so a wrong tag can be traced.

| Citation | Visa types |
|---|---|
| 8 CFR 214.2(f) | F-1, F-2 |
| 8 CFR 214.2(j), 22 CFR 62 | J-1, J-2 |
| 8 CFR 214.2(m) | M-1, M-2 |
| Policy Manual Vol. 2 Part F | by chapter heading |
| anything else | empty, meaning applies to all |

## Pipeline

Three commands, each safe to re-run.

1. `ingest fetch` downloads, validates, and stores raw responses. Validation happens
   before anything is accepted: expected content type, expected root element or page
   structure, minimum size, and no bot-check markers. A rejected response is logged with
   a reason and never parsed.
2. `ingest parse` turns accepted raw files into provisions and versions. Pure: no
   network, same input gives the same output.
3. `ingest diff` compares this run with the last and writes `changes`. Rule-change
   monitoring and alerts read this table.

Fetching rules: send `Accept-Encoding: gzip`, a descriptive user agent, at most one
request per second per host, retry with backoff on 429 and 5xx, fall back to GovInfo for
Federal Register full text.

Change detection is cheap: eCFR's `titles.json` gives each title's last amendment date
(one request), and the Federal Register can be queried by publication date since the
last run.

### Cleaning (eCFR)

- Drop "Section Contents" and "Paragraph Contents" tables. They are navigation.
- Rebuild the paragraph hierarchy from markers. Disambiguate `(i)`, `(v)`, `(x)` by
  sibling context, and test this directly.
- Split paragraphs that open more than one level, like `(ii) Optional practical
  training—(A) General.`
- Keep the heading path separate from the text. Retrieval can prepend it when chunking.
- Attach footnotes to the paragraph that references them.
- Normalize Unicode and whitespace; keep section symbols and em dashes in source text.

## History

**Decided:** backfill from 2020-01-01.

- eCFR: one full-part download per distinct amendment date. Across the four parts that
  is about 150 downloads of at most 1 MB.
- Federal Register: all documents touching the four parts since 2020, with their status.
- Policy Manual and SEVP: current version only, history accumulates from our first run.

## Guarantees we test

- Raw files are never modified after writing.
- Every provision version links to the fetch that produced it.
- Parsing the same raw file twice produces identical rows.
- A provision's validity intervals never overlap or leave gaps.
- Rejected fetches are never parsed.
- No provision text appears in logs.

## Open questions

1. Can the Jetstream2 instance reach uscis.gov and studyinthestates.dhs.gov? If SEVP
   stays blocked, do we snapshot its pages by hand?
2. Who owns chunking and Qdrant? The contract is: chunks carry `provision_id` and
   `version`, and Qdrant payload copies `authority`, `status`, `visa_types`,
   `valid_from`, `valid_to` for pre-filtering.
3. Stretch sources (IRS publications, form instructions) fit as new sources with
   `authority = guidance`; confirm before we build them.
