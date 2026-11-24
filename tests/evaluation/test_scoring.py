"""The fixed question set and the scoring rules, with canned /ask responses."""

import json
from pathlib import Path
from typing import Any

import pytest

from myapp.evaluation import __main__ as cli
from myapp.evaluation.scoring import (
    Case,
    Result,
    compare,
    load_cases,
    matches,
    report,
    score,
    summarize,
)

HOURS = Case(
    id="hours",
    kind="answer",
    question="How many hours can I work on campus?",
    visa_type="F-1",
    expect_sources=["8 CFR 214.2(f)(9)(i)"],
    forbid_sources=["8 CFR 214.2(q)*"],
    answer_has=[r"\b20\b"],
    origin="test",
)
OFF_TOPIC = Case(id="france", kind="decline", question="Capital?", origin="test")


def body(answer: str, *ids: str, status: str = "answered") -> dict[str, Any]:
    return {
        "answer": answer,
        "status": status,
        "sources": [{"provision_id": i} for i in ids],
    }


def test_question_set_is_valid() -> None:
    cases = load_cases()
    assert len(cases) >= 20
    assert {c.kind for c in cases} == {"answer", "decline"}


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "q.json"
    path.write_text(json.dumps([OFF_TOPIC.model_dump(mode="json")] * 2))
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(path)


@pytest.mark.parametrize(
    "fields",
    [
        {"expect_sources": ["(f)(9)(i)"]},  # not a full citation
        {"answer_has": ["("]},  # not a regex
        {"kind": "decline", "expect_sources": ["8 CFR 214.2(f)"]},
    ],
)
def test_bad_cases_are_rejected(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Case.model_validate(
            {"id": "x", "kind": "answer", "question": "q", "origin": "t"} | fields
        )


def test_star_matches_children_but_not_siblings() -> None:
    assert matches("8 CFR 214.2(f)(9)(i)", "8 CFR 214.2(f)(9)(i)")
    assert not matches("8 CFR 214.2(f)(9)(i)(A)", "8 CFR 214.2(f)(9)(i)")
    assert matches("8 CFR 214.2(f)(9)(i)(A)", "8 CFR 214.2(f)(9)(i)*")
    assert not matches("8 CFR 214.2(f)(9)(ii)", "8 CFR 214.2(f)(9)(i)*")


def test_answer_passes_with_source_and_text() -> None:
    result = score(
        HOURS,
        200,
        body(
            "Up to 20 hours a week [2].",
            "8 CFR 214.2(f)(9)(ii)",
            "8 CFR 214.2(f)(9)(i)",
        ),
        21.04,
    )
    assert result.passed and result.cited
    assert result.rank == 2
    assert result.seconds == 21.0


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (body("Up to 20 hours.", "8 CFR 214.2(f)(9)(ii)"), "wrong source"),
        (body("Up to 40 hours.", "8 CFR 214.2(f)(9)(i)"), "wrong text"),
        (body("20.", "8 CFR 214.2(f)(9)(i)", "8 CFR 214.2(q)(15)"), "forbidden"),
        (body("Not covered.", status="insufficient_evidence"), "declined"),
    ],
)
def test_answer_fails(response: dict[str, Any], reason: str) -> None:
    assert not score(HOURS, 200, response, 1.0).passed, reason


def test_answer_fails_without_a_response() -> None:
    result = score(HOURS, 0, None, 250.0)
    assert not result.passed and result.status is None


def test_out_of_range_labels_are_not_citations() -> None:
    result = score(HOURS, 200, body("20 hours [3].", "8 CFR 214.2(f)(9)(i)"), 1.0)
    assert result.passed and not result.cited


def test_decline_passes_on_insufficient_evidence_or_422() -> None:
    declined = body("Not covered.", status="insufficient_evidence")
    assert score(OFF_TOPIC, 200, declined, 1.0).passed
    assert score(OFF_TOPIC, 422, {"detail": []}, 0.1).passed
    assert not score(OFF_TOPIC, 200, body("Paris.", "8 CFR 214.1"), 1.0).passed


def test_summary_report_and_compare() -> None:
    before = [
        score(HOURS, 200, body("40 hours.", "8 CFR 214.2(f)(9)(ii)"), 20.0),
        score(OFF_TOPIC, 200, body("Paris."), 5.0),
    ]
    after = [
        score(HOURS, 200, body("20 hours [1].", "8 CFR 214.2(f)(9)(i)"), 20.0),
        score(OFF_TOPIC, 200, body("Paris."), 5.0),
    ]
    numbers = summarize(after)
    assert numbers["All cases passed"] == (1, 2)
    assert numbers["Correct source ranked first"] == (1, 1)
    assert numbers["Declined when it should"] == (0, 1)
    table = report(after, "test")
    assert "| hours | test | ✅ | answered | #1 | 20.0 |" in table
    diff = compare(before, after)
    assert "| All cases passed | 0/2 (0%) | 1/2 (50%) |" in diff
    assert "Now passing: hours" in diff


def test_cli_runs_cases_and_saves_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: list[dict[str, str]] = []

    def fake_post(url: str, payload: dict[str, str], timeout: float) -> tuple[int, Any]:
        sent.append(payload)
        return 200, body("Up to 20 hours [1].", "8 CFR 214.2(f)(9)(i)")

    class Ready:
        def __enter__(self) -> "Ready":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"status": "ready"}'

    monkeypatch.setattr(cli, "post", fake_post)
    monkeypatch.setattr(cli, "urlopen", lambda url, timeout: Ready())
    out = tmp_path / "run.json"
    cli.main(["--only", "oncampus-hours", "--out", str(out)])

    assert sent == [
        {"question": "How many hours can I work on campus?", "visa_type": "F-1"}
    ]
    assert "[1/1] pass oncampus-hours" in capsys.readouterr().out
    saved = json.loads(out.read_text())["results"]
    assert Result.model_validate(saved[0]).passed
