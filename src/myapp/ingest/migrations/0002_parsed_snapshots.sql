-- Expand only: older code ignores the new column, so rolling back the image is safe.
ALTER TABLE fetches ADD COLUMN parsed_by_run INTEGER REFERENCES runs;
CREATE INDEX fetches_unparsed ON fetches (accepted, parsed_by_run);
