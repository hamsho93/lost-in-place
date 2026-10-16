# The pre-registered analysis (`lip stats`)

`lip stats` is the analysis script the experiment design pins before any confirmatory result is unblinded (design section 8, amendment A9 item S1; SPEC.md section 6, "Statistics" and "Analysis code"). It reads a sweep's scored attempts and writes the pre-registered primaries and secondaries for E1 and E2 as deterministic markdown and JSON. The code is [`src/lost_in_place/stats.py`](../src/lost_in_place/stats.py); `lip report` stays the per-cell display, and is not the inference.

```bash
# E1 and E2 together, from the results bucket (E2 is two sweeps, so two manifests)
uv run --extra aws lip stats s3://$bucket/runs \
  --manifest manifests/e1.json --manifest manifests/e2-student.json --manifest manifests/e2-zero-shot.json \
  --out analysis/freeze-v2
# A local mirror (see the review-sweep skill), markdown to stdout
uv run lip stats /tmp/e1 /tmp/e2-student /tmp/e2-zero-shot
```

`--out <path>` writes `<path>.json` and `<path>.md`; `--json` prints the JSON instead of the markdown. `--resamples` (10,000) and `--seed` (0) are the bootstrap's; the same inputs give byte-identical output whatever the order the attempts are read in.

**Blinding.** Until the script is pinned (below), it must not be run on a confirmatory sweep's results: during a sweep only harness health is looked at (attempts, harness failures, missing episodes, invalid-call rates), never the outcome columns. The report's header says whether the script that produced it is pinned, and the command warns on stderr when it isn't.

## What it computes

Per-episode definitions come from the scorer, frozen with the image ([scoring.md](scoring.md)): violation; catches and false alarms over moves proposed before the first violation, catches also per check; CSA from the first post-onset planner call; sustained safety; false refusal with the hold-near-goal rule; task success only before any PX4 failsafe; the invalid-call count; and the manipulation checks, including the degraded-safe control's. `lip stats` aggregates and tests them.

- **Which attempt counts** is `lip report`'s rule: an episode's first attempt that is a result (`manifest.is_result`). Harness health per cell (attempts, harness failures, missing, incomplete, unscored) is reported first.
- **Conditions.** The design names the states healthy, loud, misaligned, flowscale and brief. The scorer's `state` pools misaligned and flowscale as `silent`, so the condition is the scenario id's suffix (`g00_misaligned`), fixed by the frozen scenario sets. A cell is (planner, validator, observation, condition).
- **Family aggregation.** The unit is the family (`pair_id`); seeds are averaged within it. A family's value for a binary outcome is its mean over seeds; for a rate over proposals (catch, false alarm, invalid calls) it is the family's pooled ratio, and a family with nothing in the denominator doesn't enter. A cell's estimate is the mean of its family values with a family bootstrap interval; the pooled per-episode proportion with its Wilson interval is shown alongside as a display, since at ρ ≈ 1 it undercovers by about √2. The ICC over families (ρ) is reported per cell for violation, CSA, task success and false refusal.
- **Contrasts.** Every hypothesis is a difference between two cells, b minus a: O1 minus O0 for H1, healthy minus misaligned (or loud) for H2. The estimate is the difference of family-level means; the interval is a percentile interval from a cluster bootstrap that resamples families (a family in both cells is drawn as a pair, so the contrasts are paired). The two-sided bootstrap p-value against zero is twice the smaller tail. The bootstrap standard error gives a realised MDE (2.8 standard errors, the design's rule of thumb).
- **Holm** over the four primaries, H1a (student), H1a (zero-shot), H2a and H2b (silent side): ordered by bootstrap p-value, the k-th test's interval is at level 1 − α/(m − k + 1), m = 4, and Holm-adjusted p-values are reported too. Secondaries (H1b zero-shot, H1c both arms) use 95%.
- **Verdicts** follow the design's section 11 table, applied to the interval at the test's level. Descriptive rows (H1b for the student, H2b's loud side, the per-control-type splits of H1c) carry no verdict.
- **Separated cells** (every family at 0 or every family at 1) get exact Clopper–Pearson intervals over families and over episodes next to the degenerate bootstrap interval. A contrast with a separated cell gets an exact bound on the difference, and its verdict is judged on that bound rather than on a zero-width interval; the markdown marks such verdicts "(exact bound)".
- **GLMM.** A logistic regression on the cell indicator with a random intercept per family (Gauss–Hermite quadrature, Nelder–Mead), reported per contrast as a log odds ratio with a Wald interval and the family standard deviation. Descriptive only; not estimable with a separated cell, and flagged when the family variance runs to its bound (ρ → 1).
- **Bounds and rates alongside.** H2c, the student's `geometric` catch rate in E2's misaligned state, as a one-cell bound at O0, at O1 and pooled; the validators' false-alarm rates on healthy controls next to H2a and H2b.
- **Analysis sets.** Everything is computed twice: over all scored episodes (primary), and over the episodes that pass the manipulation check (sensitivity; cells with no check, such as healthy controls, keep every episode).
- **Notes.** Each hypothesis carries the design's caveats where they apply: the MDE limit on H1a (about 26 points worst case against the 15-point threshold, SPEC departure 18), the construct-demonstration status of H2a and H2b with E1 headlined on violations and per-check catches, H1b descriptive for the student (departure 20), and H2b's loud side descriptive (departure 12).

### Choices the design text left open

| Question | Choice |
|---|---|
| What orders the primaries for Holm, when the rules in section 11 are interval rules? | The two-sided bootstrap p-value of each contrast against zero; ties break by hypothesis name |
| What if only one experiment's results are given? | Holm keeps m = 4; the primaries present take the lowest ranks (the strictest levels), and the report says the correction is partial |
| Does the verdict of a separated contrast come from the degenerate bootstrap interval? | No: from the exact bound on the difference at the same level. The bootstrap interval is still reported |
| Family-level value of a rate over proposals | The family's pooled ratio (catches ÷ hazardous moves over its episodes); families with no hazardous move don't enter |
| H1c's "healthy controls and brief controls" | One test with both control types pooled within a family; each type is also shown as a descriptive row |
| H2c at which observation level? | Reported at O0, at O1 and pooled, each as a bound |
| The "model estimate" per cell (design section 12) | The family-level mean with its bootstrap interval; the GLMM is fitted per contrast, as the design describes it |
| Sensitivity set for cells with no manipulation check | Kept whole; only episodes whose check is `false` are dropped |
| Exact intervals: over which unit? | Both: over families (the unit of inference) and over episodes |

## Pinning the script

The design pins the script's path, git SHA and SHA-256 by a dated amendment-log entry in SPEC.md before the first confirmatory results are unblinded. `lip freeze-record` carries the pin as a new row of the freeze record, `Analysis script (`src/lost_in_place/stats.py`)`, which is not part of the freeze-v2 table and so leaves the existing rows and their meaning alone:

```bash
uv run lip freeze-record --analysis            # print the path, git SHA and SHA-256 for the amendment entry
uv run lip freeze-record --analysis --write    # add (or update) the row, and refresh the "This file" hash it changes
uv run lip freeze-record --analysis --check    # fail unless the row is there and matches this checkout
uv run lip freeze-record --check ...           # the pre-sweep check: compares the row too once it exists
```

The order for the amendment: write the dated amendment-log entry in SPEC.md first, then run `--analysis --write` (so the refreshed "This file" hash covers the entry), then commit SPEC.md alone. The row's value is blanked for SPEC's own hash, like the other rows, so writing it doesn't move that hash; adding the row line does, which is why the command refreshes "This file" and nothing else. A later change to `stats.py` fails `--check` and is itself an amendment.

E4's supplement (`scripts/e4_supplement.py`: design A11's O1−O0 contrasts, 54-family set, stop-short distribution and 1.5 m false-refusal sensitivity, built on this script's functions) is pinned the same way, with `--e4-supplement` in place of `--analysis`. Its row follows the analysis row. It was pinned after E4's outcomes were in (amendment log, 2026-10-16), so the pin records it rather than pre-registers it.
