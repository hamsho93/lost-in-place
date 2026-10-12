---
name: review-sweep
description: >-
  Check a finished or running sweep's results before anyone quotes them: mirror attempts,
  re-score with the current scorer, break results down by scenario, validator and rejection
  reason, and check for undefined rates, label lag, image mixing and intra-scenario correlation.
  Use after a canary or a sweep, or when asked what a sweep shows.
---

# Reviewing sweep results

`lip report` gives the per-cell table. This skill covers what the table hides. Each check below caught a real problem in the E1 pilot (`docs/pilot.md`).

## 1. Mirror, re-score and break down

```bash
S=.cursor/skills/review-sweep/scripts/breakdown.py
uv run python $S mirror s3://$bucket/runs --manifest manifests/<name>.json --out /tmp/<name> [--rescore]
uv run python $S breakdown /tmp/<name> --manifest manifests/<name>.json
uv run lip report /tmp/<name> --manifest manifests/<name>.json
```

- Use `--rescore` whenever `scoring.py` changed after the sweep's image was built. It downloads each counted attempt's ULog and scores it with this checkout, which takes about 35 s for 270 episodes. Say in the write-up that results were re-scored.
- The mirror keeps upload times as file mtimes, because the attempt rule (`report.counted`) orders attempts by time. Don't copy the mirror with tools that reset mtimes.

## 2. Checks

1. **Is every rate defined?** A catch rate needs hazardous proposals. The pilot had none outside silent faults, so catch rates could not be compared there. Look for cells where `catch` or `false_alarm` is `-` or has a tiny denominator.
2. **Does the outcome vary at all?** 0% or 100% in every cell means the scenario decides the outcome, not the arm. Read the per-scenario lines, and the `icc_*` columns in `lip report --json`. An ICC above about 0.15 means more scenarios help more than more reps.
3. **What was the vehicle doing at each violation?** The breakdown's `during` shows whether violations happened under approved or rejected moves, holds or landings, and how they were labelled. Under silent faults, every violation in the pilot happened during a decision labelled safe.
4. **Why did validators reject?** Rejection reasons, with numbers masked, show whether a validator fires on the intended signal. In the pilot, `geometric` rejected because the diverged estimate made moves too long, not because it saw a hazard.
5. **Manipulation check.** If it passes almost nothing in a state, the check or the scenario is wrong; the pilot's silent check passed 0 of 88. Compare it with `exposure_peak_csa_window_m`.
6. **Labels.** Label disagreements between the online and ULog labels should be a small share of moves (27 of 1,182 in the pilot). A jump means the truth stream is lagging; check its rate in the `.post` script.
7. **Provenance.** Results from more than one (git SHA, image digest) pair mean a mixed sweep. Report the split, and don't pool mixed results for confirmatory claims.
8. **Harness failures by image and cell.** Use `triage-harness-failures` if any cell is far above the rest.

## 3. Write-up

- Report counts with Wilson intervals, never single seeds (`AGENTS.md`).
- Separate what the numbers show from what they can't support, such as undefined rates or design limits.
- Turn design consequences into numbered recommendations for the experiment design, and send them to the coordinator. Edit the design document only if you own it.
