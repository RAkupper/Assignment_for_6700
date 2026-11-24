# Evaluation

A fixed set of questions with known answers, so we can tell whether a change makes
the app better or worse instead of trying a few questions by hand. Run it before
and after a change and compare the numbers.

The questions are in [`src/myapp/evaluation/questions.json`](../src/myapp/evaluation/questions.json).
Most come from the HW3 feedback (#46–#49), the rest are common F, M and J
questions. Each expected provision was checked against the eCFR text of 8 CFR 214
as of 2026-10-28, and `grace-period-2023` against 2023-08-01.

## Run it

Against TEAM02 or any running app (one `/ask` call per question, about 10 minutes):

```bash
uv run python -m myapp.evaluation --base-url https://<host> --out before.json
```

After a change, compare with the earlier run:

```bash
uv run python -m myapp.evaluation --base-url https://<host> --compare before.json
```

On TEAM02 itself, from the jobs container:

```bash
docker compose run --rm jobs python -m myapp.evaluation --base-url http://app:8000
```

`--only <case id>` runs one question; repeat it for more. The run waits and retries
when the app answers 429 (rate limit) with `Retry-After`.

## What counts as a pass

| Kind | Passes when |
| --- | --- |
| `answer` | the app answers, cites at least one provision in `expect_sources`, cites nothing in `forbid_sources`, the answer matches every `answer_has` regex and none of `answer_lacks` |
| `decline` | the app returns `insufficient_evidence`, or rejects the input with 422 |

The report also shows whether the correct source came first, and how many answers
had inline `[n]` citations. Neither is required for a pass.

## Add a question

Add an object to `questions.json`:

```json
{
  "id": "opt-unemployment",
  "kind": "answer",
  "question": "How many days of unemployment are allowed during OPT?",
  "visa_type": "F-1",
  "expect_sources": ["8 CFR 214.2(f)(10)(ii)(E)"],
  "answer_has": ["\\b90\\b"],
  "origin": "core"
}
```

- `expect_sources` are exact citations. A trailing `*` also matches everything
  under it: `8 CFR 214.2(f)(9)(i)*` matches `(f)(9)(i)(A)` but not `(f)(9)(ii)`.
- `answer_has` and `answer_lacks` are case-insensitive regexes. Keep them loose,
  since the model's wording changes: a number or a key word.
- Look the provision up in the eCFR text for the date you are testing, not from
  memory. Paragraph numbers moved in the 2026 F-1 rule.
- `uv run pytest tests/evaluation` checks the file is valid.
