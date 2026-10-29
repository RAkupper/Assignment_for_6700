"""Check a live RAG prototype without changing its data or logging questions/text.

Run: python scripts/smoke_rag.py --base-url http://localhost:8000
This focused smoke test is not a comprehensive accuracy evaluation.
"""

import argparse
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    def call(path: str, payload: dict[str, str] | None = None) -> object:
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            base + path, data=data, headers={"Content-Type": "application/json"}
        )
        with urlopen(request, timeout=250) as response:
            return json.load(response)

    started = time.monotonic()
    try:
        assert call("/health")["status"] == "ok", "API is not healthy"
        assert call("/ready")["status"] == "ready", "Dependencies are not ready"
        question = {
            "question": "How many hours can I work on campus?",
            "visa_type": "F-1",
        }
        expected = "8 CFR 214.2(f)(9)(i)"
        hits = call("/retrieve", question)
        assert any(hit["provision_id"] == expected for hit in hits), (
            "Expected source missing"
        )
        answer = call("/ask", question)
        assert answer.get("status") == "answered", "Generation did not return an answer"
        assert answer["answer"].strip(), "Generated answer is empty"
        assert answer["sources"], "Source evidence is missing"
        labels = [int(n) for n in re.findall(r"\[(\d+)\]", answer["answer"])]
        label_status = "absent"
        if labels:
            label_status = (
                "in range"
                if all(1 <= n <= len(answer["sources"]) for n in labels)
                else "contains invalid labels"
            )
        print(
            "PASS: health, readiness, retrieval, answer, and sources "
            f"({len(hits)} hits; {time.monotonic() - started:.1f}s)"
        )
        print(f"INFO: inline citations {label_status}; answer quality requires review.")
    except (
        AssertionError,
        HTTPError,
        URLError,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        # Do not print source contents or user questions.
        parser.exit(1, f"FAIL: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
