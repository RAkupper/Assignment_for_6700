-- Catalog tables from docs/ingestion.md. Never edit an applied migration; add a new one.

CREATE TABLE runs (
    run_id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    command TEXT NOT NULL CHECK (command IN ('fetch', 'parse')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'failed')),
    accepted INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0,
    versions_added INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE fetches (
    fetch_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs,
    url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    status_code INTEGER,
    content_type TEXT,
    sha256 TEXT,
    bytes INTEGER,
    source_version TEXT,  -- eCFR: the amendment date this snapshot represents
    raw_path TEXT,  -- relative to INGEST_DATA_DIR
    accepted INTEGER NOT NULL CHECK (accepted IN (0, 1)),
    reject_reason TEXT
);

CREATE TABLE provisions (
    provision_id TEXT PRIMARY KEY,  -- the citation, never reused for anything else
    source TEXT NOT NULL,
    parent_id TEXT,
    ordinal INTEGER NOT NULL,
    heading TEXT NOT NULL DEFAULT '',
    heading_path TEXT NOT NULL DEFAULT '',
    authority TEXT NOT NULL,
    visa_types TEXT NOT NULL DEFAULT '[]',  -- JSON array; empty means all
    visa_types_rule TEXT NOT NULL DEFAULT ''
);

CREATE TABLE provision_versions (
    provision_id TEXT NOT NULL REFERENCES provisions,
    version INTEGER NOT NULL,
    text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('in_force', 'pending', 'proposed', 'superseded', 'withdrawn')),
    valid_from TEXT,  -- null when in force before the history window starts
    valid_to TEXT,  -- null while still in force
    source_document TEXT,
    fetch_id INTEGER NOT NULL REFERENCES fetches,
    PRIMARY KEY (provision_id, version)
);

CREATE UNIQUE INDEX one_open_version ON provision_versions (provision_id)
    WHERE valid_to IS NULL;

CREATE TABLE changes (
    change_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs,
    provision_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('added', 'modified', 'removed')),
    effective TEXT,
    old_hash TEXT,
    new_hash TEXT
);

CREATE INDEX changes_by_run ON changes (run_id);
