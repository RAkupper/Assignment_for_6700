"""Ingestion commands. Each is safe to rerun.

python -m myapp.ingest.catalog migrate     # first, and after pulling new code
python -m myapp.ingest fetch               # every configured eCFR part
python -m myapp.ingest fetch --part "8 CFR 214"
python -m myapp.ingest parse               # apply new snapshots as versions
python -m myapp.ingest parse --rebuild     # after a parser fix: reapply all from raw
"""

import argparse
import logging
from collections.abc import Sequence
from contextlib import closing

from myapp.config import IngestSettings
from myapp.ingest.catalog import backup, catalog_path, connect, require_current
from myapp.ingest.ecfr import EcfrSource, split_part
from myapp.ingest.fetch import already_fetched, fetch
from myapp.ingest.http import PoliteClient
from myapp.ingest.versions import parse, reset


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fetch_cmd = sub.add_parser("fetch", help="download and store eCFR snapshots")
    fetch_cmd.add_argument("--part", action="append", help='e.g. "8 CFR 214"')
    parse_cmd = sub.add_parser("parse", help="turn fetched snapshots into versions")
    parse_cmd.add_argument("--part", action="append", help='e.g. "8 CFR 214"')
    parse_cmd.add_argument(
        "--rebuild", action="store_true", help="reapply all from raw"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # one line per request

    settings = IngestSettings()
    client = PoliteClient(settings)
    try:
        with closing(connect(catalog_path(settings))) as con:
            require_current(con)
            for part in args.part or settings.ecfr_parts:
                source = EcfrSource(
                    settings, client, part, lambda url: already_fetched(con, url)
                )
                if args.command == "fetch":
                    fetch(con, settings.data_dir, source)
                    continue
                title, number = split_part(part)
                url_part = f"title-{title}.xml?part={number}"
                prefix = f"{title} CFR {number}."
                if args.rebuild:
                    saved = backup(con, settings.data_dir / "backups", "rebuild")
                    logging.info("backed up to %s", saved)
                    logging.info("reset %d snapshots", reset(con, url_part, prefix))
                summary = parse(
                    con,
                    settings.data_dir,
                    source,
                    url_part=url_part,
                    prefix=prefix,
                    baseline_day=settings.ecfr_since.isoformat(),
                )
                logging.info(
                    "parse run %d: %d snapshots, %d added, %d modified, %d removed",
                    summary.run_id,
                    summary.snapshots,
                    summary.added,
                    summary.modified,
                    summary.removed,
                )
    finally:
        client.close()


if __name__ == "__main__":
    main()
