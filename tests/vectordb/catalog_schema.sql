-- Minimal catalog from docs/ingestion.md: only the columns the loader reads.
CREATE TABLE runs (run_id INTEGER PRIMARY KEY, source TEXT, status TEXT);
CREATE TABLE fetches (fetch_id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES runs);
CREATE TABLE provisions (
    provision_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    heading_path TEXT,
    authority TEXT NOT NULL,
    visa_types TEXT  -- JSON array
);
CREATE TABLE provision_versions (
    provision_id TEXT REFERENCES provisions,
    version INTEGER NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    valid_from TEXT,  -- null when the source gives no date
    valid_to TEXT,
    fetch_id INTEGER REFERENCES fetches,
    PRIMARY KEY (provision_id, version)
);
