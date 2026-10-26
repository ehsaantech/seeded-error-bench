# A seeded-error benchmark for fixed-asset substantive testing

**Two arms, one ground truth, one scorer: deterministic rules against a language model,
on synthetic fixed-asset fixtures whose errors were written down *before* the fixtures
were.**

Everything in this repository is synthetic. There is no client material here, and there
is nothing to anonymise.

<!-- RESULTS -->

---

## Why this exists

A demo of an agent finding errors is not evidence. A demo of an agent finding errors
*next to the same errors found by rules, on a population where the ground truth is
known, with the misses published* is.

The thesis this repository is built to test is narrow and falsifiable:

> The model is good at reading. The model is not better than rules at arithmetic over
> numbers that are already in a structured place — and the difference shows up as
> auditor-minutes, not as a benchmark score.

## The method

### The population

`python -m tiepoint_bench build --count 40 --seed 7` writes 40 synthetic fixed-asset
engagements. Each one is a register, a prior-year rollforward, a GL extract, a trial
balance, a depreciation policy, and the invoices and disposal approvals that support
the register's movements.

**The ground truth is written down before the fixture is.** The generator computes the
truth — the rollforwards that balance, the policy charge, the ledger that agrees — then
corrupts it in *named* ways, then records what the truth was. One seeded error is one
true finding: the corruptions are chosen so that nothing cascades, because a cascade
makes the ground truth a guess about what else would have followed.

The corruptions are the seven a fixed-asset reviewer actually looks for:

| type | the error |
|---|---|
| `dep_variance` | the charge for a class is not the policy charge (rate × closing cost) |
| `cost_variance` | the ledger (GL or trial balance) does not agree with the register's closing cost |
| `rollforward_variance` | the register's own movements do not reconcile to its closing cost |
| `prior_year_variance` | the opening balance is not the prior year's closing balance |
| `missing_support` | an addition has no supporting invoice |
| `support_variance` | the invoice for an addition states a different amount from the register |
| `missing_approval` | a disposal has no approved disposal document |

16 of the 40 fixtures are **clean** — no error seeded. They are what make precision
falsifiable: without a population with nothing wrong in it, "no false positives" means
nothing.

The population is **byte-stable**: the same seed is the same fixtures, the same bytes,
every time. (Both openpyxl and python-docx stamp the clock into the file they write;
the generator rewrites the archives with a fixed timestamp, so a regenerated fixture has
the same hash.)

### The two arms

**The deterministic arm** is the fixed-asset checks: rules over the documents, a
locator on every figure, and **no model call, ever**. A figure summed from several
cells names them all rather than claiming one, so the shape of the locator records how
many cells the number came from.

**The LLM arm** is the same documents as text, and the model's judgement: it is given
the firm's checklist and asked what is wrong. It is given no help the rules do not get
— the same policy document, the same figures, the same question. The comparison is the
decision path and nothing else.

### The scoring rules

Published, because a scorer that can flatter is not a scorer.

- A finding **matches** a ground-truth error when its type is the same (after a
  published synonym map) and it is about the same thing — a reference (`FA-0102`,
  `DISP-2026-003`) if the finding names one, otherwise the normalised name.
- A finding that matches an error nobody has claimed yet is a **true positive**, and
  marks that error **found**.
- A second finding on an error already found is a **duplicate**: not wrong, not enough.
  It is reported, and it is neither a true positive nor a false positive. (The ledger
  error is in the GL *and* in the trial balance, so a correct check reports it twice —
  that is one error and two reports, not two errors.)
- A finding that matches nothing is a **false positive**. So is every finding on a
  clean fixture.
- **precision** = found / (found + false positives) · **recall** = found / seeded.

Names are **never** matched by containment. `Forklift` and `Forklift replacement` are
different assets, and a matcher that treats one as the other is exactly the false
positive this benchmark exists to expose.

### The auditor-minutes row

The commercially meaningful one, and an **assumption, stated so it can be re-computed**:

- a located, typed finding takes **2 minutes** to clear — open the cell, agree, sign;
- a claim without a locator takes **8 minutes** — find the source yourself, then agree.

A finding is *located* when it points at a cell, a row, a page, or a named field of a
named document. "The register disagrees with the GL" names two files, which is a place
to start looking, not a place to sign.

The absolute numbers depend on those constants. The ratio is the claim, and it holds
for any positive per-item cost.

## What this benchmark does **not** claim

- **One population**: fixed assets. One template. 6 of 8 workpaper sheets.
- **One error taxonomy**: the seven above. A different population has a different list.
- **Synthetic documents**: flat, tidy, written by a generator. Real registers are
  messier, and a real engagement has judgement, disputes and missing evidence.
- **A model, not a market**: one model on one day, temperature 0, one prompt. The point
  is the *shape* of the comparison, not a leaderboard.

## Running it

```bash
pip install -e .
python -m tiepoint_bench build --count 40 --seed 7      # the population + the ground truth
python -m tiepoint_bench deterministic                   # the rules arm
python -m tiepoint_bench llm --provider cloud --model glm-5.3-flash   # the model arm
pytest                                                   # the tests, including the scorer's rules
```

The LLM arm needs an OpenAI-compatible endpoint. It uses `AI_BASE_URL`, `AI_API_KEY`
and `AI_MODEL` from the environment, or `--endpoint`, `--api-key` and `--model`.

## The misses

Published with the hits, because a benchmark that only reports wins is marketing. The
run record in `results/` carries every fixture's score, every miss, and every false
positive, with the model's own note for each.

## Licence

MIT. The benchmark harness, the fixture generator and the checks are open to reuse —
that is the point of publishing them. Nothing here is client material.
