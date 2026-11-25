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

## Results

Full runs on TEAM02 through `scripts/deploy.sh`, with the same 25 questions and the
same data (8 CFR 214, 3,882 provision versions).

| Metric | v0.3.0 (Nov 24) | v0.4.0 (Nov 24) | 0.5.0 prompt (Nov 25, local) |
| --- | --- | --- | --- |
| All cases passed | 19/25 | 22/25 | 23/25 |
| Correct source retrieved | 16/20 | 18/20 | 18/20 |
| Correct source ranked first | 13/20 | 16/20 | 16/20 |
| Answers that passed | 16/21 | 18/21 | 19/21 |
| Declined when it should | 3/4 | 4/4 | 4/4 |
| Answers with inline citations | 0/19 | 0/20 | 19/20 |
| Median seconds per question | 18.4 | 13.6 | 20.8 on a slower local CPU (v0.4.0 there: 18.2) |

v0.4.0 added the cross-encoder reranker (#57, cause 3). Three cases flipped to pass:

- `form-i983`: BM25 already found the training-plan rules, but their dense cosine
  (0.58) was under the 0.65 cutoff, so the app declined. The reranker scores them
  first.
- `m1-work`: the M-1 employment rule was 5th, outside the top 3. It is now first.
- `too-vague`: "Program limitations?" now scores below the reranker's cutoff and is
  declined instead of answered with an unrelated rule.

The 0.5.0 column was measured on a local copy of the stack (same model, data and
questions; v0.4.0 scored the same 22/25 there) before deploying. The new prompt
numbers the evidence, requires a source number on every sentence, states the
asker's visa, and flags visa classes that do not exist, which fixed `f4-stem-opt`
and took inline citations from 0 to 19 of 20.

Still failing in v0.4.0, and why:

| Case | Cause | Fix |
| --- | --- | --- |
| `opt-length` | The 12-month limit is inside the long (f)(10) paragraph, which ranks 48th on dense search and never reaches the candidates. | Split long provisions or search pieces and return the section (#57, cause 1). |
| `f4-stem-opt` | The model answers yes for F-4, a class that does not exist. The visa type never reaches the prompt. | Fixed in 0.5.0: the visa type is in the prompt and nonexistent classes are flagged. |
| `degree-years` | The tester's wording ("how many years can I work on my Bachelor's") matches no provision well; every candidate scores about -6. | Declining is defensible. A clarifying question would help (deferred in #57). |

In v0.4.0 no answer had inline `[n]` citations; 0.5.0 fixed that (19 of 20).

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
