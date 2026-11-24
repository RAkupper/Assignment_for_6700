"""Score /ask responses against questions.json. Pure: no network, no clock.

An "answer" case passes when the app answers, cites at least one expected
provision, cites none of the forbidden ones, and its text passes the regex checks.
A "decline" case passes when the app says the rules don't cover the question, or
rejects the input with 422.
"""

import json
import re
import statistics
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

QUESTIONS = Path(__file__).with_name("questions.json")
CITATION = re.compile(r"^\d+ CFR [\w.]+(\([^()]+\))*\*?$")


class Case(BaseModel):
    """One question and what a correct response looks like."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9-]+$")
    kind: Literal["answer", "decline"]
    question: str = Field(min_length=1)
    visa_type: str | None = None
    as_of: date | None = None
    # Exact citations; a trailing "*" also matches everything under it.
    expect_sources: list[str] = []
    forbid_sources: list[str] = []
    answer_has: list[str] = []  # case-insensitive regexes that must all match
    answer_lacks: list[str] = []  # and none of these
    origin: str  # where the question came from: "#47.1", "page example", "core"
    note: str = ""

    @model_validator(mode="after")
    def check(self) -> "Case":
        for pattern in self.expect_sources + self.forbid_sources:
            if not CITATION.match(pattern):
                raise ValueError(f"{self.id}: not a citation: {pattern!r}")
        for pattern in self.answer_has + self.answer_lacks:
            try:
                re.compile(pattern)
            except re.error as exc:  # not a ValueError, so pydantic would miss it
                raise ValueError(f"{self.id}: bad regex {pattern!r}: {exc}") from exc
        if self.kind == "decline" and (self.expect_sources or self.answer_has):
            raise ValueError(f"{self.id}: a decline case expects no sources")
        return self


class Result(BaseModel):
    """What the app returned for one case, and whether that passes."""

    id: str
    kind: Literal["answer", "decline"]
    origin: str
    expects_sources: bool  # the case lists expect_sources, so rank is graded
    http_status: int  # 0: no response (timeout or connection error)
    status: str | None  # Answer.status, when the app answered
    rank: int | None  # 1-based position of the first expected source
    forbidden: list[str]  # forbidden provisions the app cited anyway
    text_ok: bool  # answered, and answer_has / answer_lacks hold
    cited: bool  # the answer has inline [n] labels, all in range
    passed: bool
    seconds: float
    answer: str
    sources: list[str]


def load_cases(path: Path = QUESTIONS) -> list[Case]:
    cases = [Case.model_validate(c) for c in json.loads(path.read_text())]
    ids = [c.id for c in cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"duplicate case ids: {duplicates}")
    return cases


def matches(provision_id: str, pattern: str) -> bool:
    """Exact citation, or with a trailing "*" the citation and its children.

    "(f)(9)(i)*" matches "(f)(9)(i)(A)" but not "(f)(9)(ii)".
    """
    if pattern.endswith("*"):
        base = pattern[:-1]
        return provision_id == base or provision_id.startswith(base + "(")
    return provision_id == pattern


def score(case: Case, http_status: int, body: Any, seconds: float) -> Result:
    """Turn one /ask response (parsed JSON, or None) into a Result."""
    ok = http_status == 200 and isinstance(body, dict)
    status = body.get("status") if ok else None
    answer = body.get("answer", "") if ok else ""
    sources = [s["provision_id"] for s in body.get("sources", [])] if ok else []

    rank = next(
        (
            n
            for n, pid in enumerate(sources, 1)
            if any(matches(pid, p) for p in case.expect_sources)
        ),
        None,
    )
    forbidden = [
        pid for pid in sources if any(matches(pid, p) for p in case.forbid_sources)
    ]
    text_ok = (
        status == "answered"
        and all(re.search(p, answer, re.I | re.M) for p in case.answer_has)
        and not any(re.search(p, answer, re.I | re.M) for p in case.answer_lacks)
    )
    labels = [int(n) for n in re.findall(r"\[(\d+)\]", answer)]
    cited = bool(labels) and all(1 <= n <= len(sources) for n in labels)

    if case.kind == "decline":
        passed = http_status == 422 or status == "insufficient_evidence"
    else:
        found = rank is not None or not case.expect_sources
        passed = text_ok and found and not forbidden
    return Result(
        id=case.id,
        kind=case.kind,
        origin=case.origin,
        expects_sources=bool(case.expect_sources),
        http_status=http_status,
        status=status,
        rank=rank,
        forbidden=forbidden,
        text_ok=text_ok,
        cited=cited,
        passed=passed,
        seconds=round(seconds, 1),
        answer=answer,
        sources=sources,
    )


def summarize(results: list[Result]) -> dict[str, tuple[int, int]]:
    """Headline numbers as (passed, total) pairs."""
    answers = [r for r in results if r.kind == "answer"]
    sourced = [r for r in answers if r.expects_sources]
    answered = [r for r in answers if r.status == "answered"]
    declines = [r for r in results if r.kind == "decline"]
    return {
        "All cases passed": _count(results, lambda r: r.passed),
        "Correct source retrieved": _count(sourced, lambda r: r.rank is not None),
        "Correct source ranked first": _count(sourced, lambda r: r.rank == 1),
        "Answers that passed": _count(answers, lambda r: r.passed),
        "Answers with inline citations": _count(answered, lambda r: r.cited),
        "Declined when it should": _count(declines, lambda r: r.passed),
    }


def _count(results: list[Result], test: Callable[[Result], bool]) -> tuple[int, int]:
    return sum(1 for r in results if test(r)), len(results)


def _ratio(pair: tuple[int, int]) -> str:
    passed, total = pair
    return f"{passed}/{total} ({passed / total:.0%})" if total else "n/a"


def report(results: list[Result], title: str) -> str:
    """Markdown: the headline numbers, then one row per case."""
    seconds = [r.seconds for r in results if r.http_status]
    lines = [f"## {title}", "", "| Metric | Result |", "| --- | --- |"]
    lines += [f"| {k} | {_ratio(v)} |" for k, v in summarize(results).items()]
    if seconds:
        lines.append(f"| Median seconds per question | {statistics.median(seconds)} |")
    lines += [
        "",
        "| Case | From | Pass | Status | First correct source | Seconds |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        status = r.status or f"HTTP {r.http_status}"
        rank = f"#{r.rank}" if r.rank else ("-" if not r.expects_sources else "none")
        if r.forbidden:
            rank += f", cited {', '.join(r.forbidden)}"
        mark = "✅" if r.passed else "❌"
        lines.append(
            f"| {r.id} | {r.origin} | {mark} | {status} | {rank} | {r.seconds} |"
        )
    return "\n".join(lines)


def compare(before: list[Result], after: list[Result]) -> str:
    """Markdown: headline numbers side by side, and the cases that changed."""
    old, new = summarize(before), summarize(after)
    lines = ["| Metric | Before | After |", "| --- | --- | --- |"]
    lines += [f"| {k} | {_ratio(old[k])} | {_ratio(new[k])} |" for k in new]
    was = {r.id: r.passed for r in before}
    fixed = [r.id for r in after if r.passed and was.get(r.id) is False]
    broke = [r.id for r in after if not r.passed and was.get(r.id) is True]
    lines += ["", f"Now passing: {', '.join(fixed) or 'none'}"]
    lines += [f"Now failing: {', '.join(broke) or 'none'}"]
    return "\n".join(lines)
