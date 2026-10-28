# Ingestion

How regulations and guidance get from federal websites into a form the rest of the
app can trust: retrieval, keyword search, change monitoring, and evaluation.

Status: design. Decisions agreed so far are marked **Decided**; open questions are at
the end.

## What the sources actually look like

Checked against live responses on 2026-10-25.

| Source | What we pull | Access | What we found |
|---|---|---|---|
| eCFR | 8 CFR 214, 8 CFR 274a, 22 CFR 41, 22 CFR 62 | Versioner API, XML | Requires `Accept-Encoding: gzip`. 8 CFR 214 is ~150k words, 115k of them in §214.2. Paragraphs are flat `<P>` elements; hierarchy exists only as text markers like `(ii) ...—(A)`, up to seven levels deep. `(i)` can be a letter or a roman numeral. Dated versions back to 2017. |
| Federal Register | Documents touching those parts | API, JSON metadata and full text | 68 documents on 8 CFR 214 since 2020, including **proposed** rules (OPT fees, 60-day grace period). Rules run ~150 pages, mostly preamble. Full text works from Jetstream2; from other networks it can return a bot-check page **with status 200**, so responses are validated and GovInfo is the fallback. CFR part filter takes integers only (274a must be queried as 274). |
| USCIS Policy Manual | Vol. 2 Part F (students) and related chapters | HTML, **manual capture** | Chapters ~9k words, lettered and numbered headings, an update date, dozens of footnotes citing 8 CFR paragraphs. Blocks automated access from Jetstream2 (Akamai "Access Denied"). No history API. |
| SEVP (Study in the States) | Student guidance pages | HTML, **manual capture** | Returns 403 to automated requests from Jetstream2. |

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

All of it lives under `INGEST_DATA_DIR`. On the instance that is a dedicated Jetstream2
volume (TEAM02's root disk has ~3.7 GB free), bind-mounted into containers at `/data`,
and
ignored by git and Docker.

```
data/
  raw/<source>/<yyyy-mm-dd>/<sha256>.<ext>.gz   content-addressed, so re-fetching the same bytes stores nothing new
  catalog.sqlite
```

**Decided:** SQLite for the catalog. One file, no server, transactions. It is the
source of truth; Qdrant is derived from it and can be rebuilt from it at any time.

## Catalog tables

| Table | One row per | Key columns |
|---|---|---|
| `runs` | ingestion run | `run_id`, `source`, `started_at`, `finished_at`, `status`, counts |
| `fetches` | HTTP response | `fetch_id`, `run_id`, `url`, `status_code`, `content_type`, `sha256`, `bytes`, `source_version`, `raw_path`, `accepted`, `reject_reason` |
| `provisions` | citable unit (identity only, never changes) | `provision_id` (the citation), `source`, `parent_id`, `ordinal`, `heading`, `heading_path`, `authority`, `visa_types`, `visa_types_rule` |
| `provision_versions` | distinct text of a provision over time | `provision_id`, `version`, `text`, `content_hash`, `valid_from`, `valid_to`, `status`, `source_document`, `fetch_id` |
| `references` | citation found inside a provision | `from_provision_id`, `from_version`, `to_citation`, `to_provision_id` (null if not in our corpus) |
| `changes` | detected difference between runs | `run_id`, `provision_id`, `kind` (added, modified, removed), `old_hash`, `new_hash` |

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
| 8 CFR 214.3, 214.4, 214.12 (school certification) | F-1, F-2, M-1, M-2 |
| 8 CFR 214.13 (SEVIS fee) | F-1, J-1, M-1 |
| Other lettered paragraphs of 8 CFR 214.2 | their class: (a) A, (b) B, (c) C, (d) D, (e) E, (g) G, (h) H, (i) I, (k) K, (l) L, (n) N, (o) O, (p) P, (q) Q, (r) R, (s) NATO, (t) S, (v) V, (w) CW |
| 8 CFR 214.5, 214.6, 214.7 | LIBYA, TN, FAS |
| 8 CFR 214.11, 214.200 to 214.216 | T |
| 8 CFR 214.14, 214.15 | U, V |
| Policy Manual Vol. 2 Part F | by chapter heading |
| anything else (e.g. 8 CFR 214.1) | empty, meaning applies to all |

Every other visa class is tagged so that a student's filtered search excludes rules
for, say, H-1B workers, while an unfiltered search still sees everything. The
paragraph letters of 214.2 were checked against their headings in every snapshot
since 2020.

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

## Handoff to retrieval (Qdrant)

**Decided:** Qdrant is the vector database. Ingestion ends at `catalog.sqlite`;
chunking, embedding, and loading belong to the retrieval owner. This section is the
contract between the two.

What ingestion guarantees:

- Every provision version in `provision_versions` is complete and validated when its
  run's `status` is `succeeded`. Read only from succeeded runs.
- `changes` lists every added, modified, and removed provision per run, so the loader
  can sync incrementally instead of reloading everything.
- `provision_id` and `version` never change meaning once written.

What the loader is expected to do:

- **Load every version, not just current ones.** History is part of the product. The
  default query filter is "in force now" (`valid_to` is null); "as of date X" is a date
  filter on the same collection.
- **Use deterministic point ids**: a UUIDv5 of `provision_id`, `version`, and chunk
  number. Re-running a load updates points in place instead of duplicating them.
- **Copy these fields into the payload** and create a payload index on each before
  loading, so filters run before vector search:

  | Field | Qdrant index type |
  |---|---|
  | `provision_id` | keyword |
  | `source` | keyword |
  | `authority` | keyword |
  | `status` | keyword |
  | `visa_types` | keyword (array) |
  | `valid_from`, `valid_to` | datetime |

- **Keep keyword search in Qdrant**: store a sparse BM25 vector next to the dense one
  and fuse them in one query, so the same filters apply to both. Exact terms like
  "I-983" or "cap-gap" need this.
- **Sync from `changes`** after each succeeded run: upsert added and modified, delete
  removed. Take a Qdrant snapshot before each sync; rolling back the app image does not
  roll back data.
- **Chunks never cross provision boundaries.** A long provision may split into several
  chunks; a chunk never mixes two provisions, so every answer cites one exact paragraph.

Deployment: a `qdrant` service in `compose.yml` (#37). Its storage, `/data`, and the
embedding cache are bind mounts under `DATA_VOLUME_DIR`, the 20 GB Jetstream2 volume,
not named volumes: those live on TEAM02's root disk, which has ~3.7 GB free.

## Manually captured sources

USCIS and Study in the States block automated access, and we do not work around
that. Instead, a fixed list of pages is saved from a normal browser into
`manual/<source>/` with a manifest:

```
url, captured_at, captured_by, sha256, filename
```

`ingest fetch` copies these into `raw/` like any other response, so parsing, history,
and change detection work the same. Their fetch rows are marked `manual`. Re-capture
monthly, or when a Federal Register rule touches what they cover.

## Open questions

1. Who does the manual capture, and which Policy Manual chapters and Study in the
   States pages are on the list?
2. Which teammate owns chunking and the Qdrant loader, and do they agree with the
   handoff contract above?
3. Can we create a 20 GB volume for `/data` on the shared allocation?
4. Stretch sources (IRS publications, form instructions) fit as new sources with
   `authority = guidance`; confirm before we build them.
