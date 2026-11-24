"""Run the fixed questions against a live app and print a Markdown report.

From a laptop:
    uv run python -m myapp.evaluation --base-url https://<host> --out before.json
On TEAM02, from the jobs container:
    docker compose run --rm jobs python -m myapp.evaluation --base-url http://app:8000

Pass --compare before.json to put an earlier run next to this one. Each question
takes one /ask call, so a full run takes about as long as 25 answers (~10 min).
"""

import argparse
import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from myapp.evaluation.scoring import Case, Result, compare, load_cases, report, score

MAX_WAITS = 3  # retries after a 429 or 503 that says when to come back


def post(url: str, payload: dict[str, str], timeout: float) -> tuple[int, Any]:
    """POST JSON; return the status and the parsed body (None if not JSON)."""
    data = json.dumps(payload).encode()
    request = Request(url, data=data, headers={"Content-Type": "application/json"})
    for attempt in range(MAX_WAITS + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            wait = exc.headers.get("Retry-After", "")
            if exc.code in (429, 503) and wait.isdigit() and attempt < MAX_WAITS:
                time.sleep(int(wait))  # rate limited or busy: the app said when
                continue
            try:
                return exc.code, json.load(exc)
            except ValueError:
                return exc.code, None
        except (URLError, TimeoutError):
            return 0, None
    raise AssertionError("unreachable")  # the last attempt returns


def run_case(base: str, case: Case, timeout: float) -> Result:
    payload = {"question": case.question}
    if case.visa_type:
        payload["visa_type"] = case.visa_type
    if case.as_of:
        payload["as_of"] = case.as_of.isoformat()
    started = time.monotonic()
    status, body = post(f"{base}/ask", payload, timeout)
    return score(case, status, body, time.monotonic() - started)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Score a live app on fixed questions.")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--out", type=Path, help="save the results as JSON")
    parser.add_argument("--compare", type=Path, help="an earlier --out file")
    parser.add_argument("--only", action="append", help="run only this case id")
    parser.add_argument("--timeout", type=float, default=250.0)
    args = parser.parse_args(argv)
    base = args.base_url.rstrip("/")

    cases = load_cases()
    if args.only:
        cases = [c for c in cases if c.id in args.only]
    try:
        with urlopen(f"{base}/ready", timeout=20) as response:
            ready = json.load(response).get("status") == "ready"
    except (HTTPError, URLError, TimeoutError, ValueError):
        ready = False
    if not ready:
        parser.exit(1, f"{base}/ready is not ready; start the app first\n")

    started = datetime.now(UTC)
    results = []
    for n, case in enumerate(cases, 1):
        result = run_case(base, case, args.timeout)
        mark = "pass" if result.passed else "FAIL"
        print(f"[{n}/{len(cases)}] {mark} {case.id} ({result.seconds}s)", flush=True)
        results.append(result)

    print()
    print(report(results, f"{base} · {started:%Y-%m-%d %H:%M} UTC"))
    if args.compare:
        before = json.loads(args.compare.read_text())["results"]
        print()
        print(compare([Result.model_validate(r) for r in before], results))
    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "base_url": base,
                    "started_at": started.isoformat(),
                    "results": [r.model_dump() for r in results],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
