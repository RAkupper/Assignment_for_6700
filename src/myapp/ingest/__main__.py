"""Ingestion commands. Each is safe to rerun.

python -m myapp.ingest.catalog migrate     # first, and after pulling new code
python -m myapp.ingest fetch               # every configured eCFR part
python -m myapp.ingest fetch --part "8 CFR 214"
"""

import argparse
import logging
from collections.abc import Sequence
from contextlib import closing

from myapp.config import IngestSettings
from myapp.ingest.catalog import catalog_path, connect, require_current
from myapp.ingest.ecfr import EcfrSource
from myapp.ingest.fetch import already_fetched, fetch
from myapp.ingest.http import PoliteClient


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fetch_cmd = sub.add_parser("fetch", help="download and store eCFR snapshots")
    fetch_cmd.add_argument("--part", action="append", help='e.g. "8 CFR 214"')
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
                fetch(con, settings.data_dir, source)
    finally:
        client.close()


if __name__ == "__main__":
    main()
