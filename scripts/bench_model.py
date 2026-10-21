"""Measure model latency through the service's own client.

Reads the same ``LLM_*`` variables as the app, so it benchmarks exactly what the
service will call. Run it on the deployment host against the real model server:

    LLM_PROVIDER=openai LLM_BASE_URL=http://localhost:11434/v1 \\
        uv run python scripts/bench_model.py --sequential 5 --concurrent 10
"""

import argparse
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from myapp.config import ModelSettings
from myapp.model_client import OpenAICompatibleClient

PROMPT = (
    "In two sentences, explain when an F-1 student may apply for post-completion "
    "OPT and whether they may work before the application is approved."
)


def timed(client: OpenAICompatibleClient) -> float:
    """Return the wall-clock seconds one generate() call takes."""
    start = time.perf_counter()
    client.generate(PROMPT)
    return time.perf_counter() - start


def main() -> None:
    """Run a warm-up call, then sequential and concurrent rounds."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sequential", type=int, default=5)
    parser.add_argument("--concurrent", type=int, default=10)
    args = parser.parse_args()

    settings = ModelSettings(provider="openai")
    client = OpenAICompatibleClient(settings)
    print(f"model={settings.model} base_url={settings.base_url}")

    cold = timed(client)  # first call also loads weights into the server's memory
    print(f"first call (includes server-side model load): {cold:.2f} s")

    latencies = sorted(timed(client) for _ in range(args.sequential))
    print(
        f"sequential n={args.sequential}: median={statistics.median(latencies):.2f} s "
        f"max={latencies[-1]:.2f} s"
    )

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrent) as pool:
        results = list(pool.map(lambda _: timed(client), range(args.concurrent)))
    total = time.perf_counter() - start
    print(
        f"concurrent n={args.concurrent}: total={total:.2f} s "
        f"slowest={max(results):.2f} s"
    )
    client.close()


if __name__ == "__main__":
    main()
