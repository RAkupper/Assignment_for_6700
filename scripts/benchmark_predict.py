"""Measure ten concurrent HTTP requests against a running /predict endpoint."""

import argparse
import asyncio
import json
from pathlib import Path
from time import perf_counter

import httpx

REQUEST_COUNT = 10
PAYLOAD = {"pixels": [0] * 64}


async def send_one(client: httpx.AsyncClient, url: str) -> float:
    """Time one complete request, including receiving the response body."""
    start = perf_counter()
    response = await client.post(url, json=PAYLOAD)
    elapsed = perf_counter() - start
    response.raise_for_status()
    return elapsed


async def benchmark(url: str, output: Path | None) -> None:
    """Run ten requests concurrently and report individual and batch timings."""
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        # Create ten coroutines; this loop does not send requests one by one.
        requests = [send_one(client, url) for _ in range(REQUEST_COUNT)]

        # Start the batch clock just before scheduling all ten requests.
        start = perf_counter()
        durations = await asyncio.gather(*requests)
        wall_clock = perf_counter() - start  # All ten responses have arrived.

    slowest_index = max(range(REQUEST_COUNT), key=lambda i: durations[i])
    for number, duration in enumerate(durations, start=1):
        print(f"Request {number:02d}: {duration:.6f} s")
    print(f"Wall-clock time: {wall_clock:.6f} s")
    print(f"Slowest request: #{slowest_index + 1} ({durations[slowest_index]:.6f} s)")

    if output is not None:
        result = {
            "url": url,
            "request_count": REQUEST_COUNT,
            "payload": PAYLOAD,
            "request_seconds": durations,
            "wall_clock_seconds": wall_clock,
            "slowest_request_number": slowest_index + 1,
            "slowest_request_seconds": durations[slowest_index],
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"Saved measurements to {output}")


def main() -> None:
    """Read CLI options and start the client's event loop."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000/predict")
    parser.add_argument("--output", type=Path, help="Save measurements as JSON")
    args = parser.parse_args()
    asyncio.run(benchmark(args.url, args.output))


if __name__ == "__main__":
    main()
