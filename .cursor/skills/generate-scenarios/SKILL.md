---
name: generate-scenarios
description: >-
  Generate E1 scenario families (healthy, misaligned, flowscale, loud) with `lip families`, check
  them statically, and fly one family in the sim to confirm each state behaves as designed. Use
  when adding scenario families, changing the generator in `families.py`, or when a generated
  family's episodes look wrong.
---

# Generate scenarios

A family is one spawn, one two-leg mission and up to three keep-outs, flown in four states. `src/lost_in_place/families.py` draws families from a seed and keeps only those that pass `problems()`, which checks the design's claims at every extreme of the spawn jitter.

## 1. Generate

```bash
uv run lip families --count 12 --start 0 --prefix g --seed 0 --out scenarios/e1
```

The same name and seed always give the same family. To add families, raise `--start` rather than changing `--seed`, so existing files don't change. Families in `scenarios/e1/` are checked by `tests/test_e1_scenarios.py`; run `uv run pytest tests/test_e1_scenarios.py -q`.

## 2. Fly one family

Static checks use straight-line geometry. Fly one family locally to confirm the vehicle does what they assume. Run two states at a time on separate instances; the mounted directory must be `chmod 777`.

```bash
uv run lip families --count 1 --out /tmp/fam && chmod -R 777 /tmp/fam
docker run --rm -v $PWD/src:/opt/lost-in-place/src -v /tmp/fam:/x lost-in-place \
  run /x/g00_misaligned.yaml --seed 1 --instance 0 --validator geometric --out /x/out/mis --analyze
```

## 3. What each state should show

With `scripted-naive` and `geometric`, in `score.json` and the `decisions` in `episode.json`:

| State | Expected |
|---|---|
| healthy | No violation. `geometric` rejects the first move of the second leg: the instruction flies into a keep-out on purpose, so this rejection is the healthy catch, not a generator bug |
| misaligned | A violation of the keep-out beside the first leg (named first in the notes). The first-leg moves before it are labelled hazardous and approved, since `geometric` can't see the misalignment |
| flowscale | A violation of the keep-out beyond the first leg. The first-leg moves are labelled safe, because labels judge the commanded segment from the believed position |
| loud | A failsafe about 10 s after the flow cuts out (`EKF2_NOAID_TOUT`), and usually no violation |

`label_disagreements` should be 0 in every state. If a state differs, compare the truth track (`lip analyze`) with what `problems()` assumed: `overshoot_end()` for flowscale, and `jitter_extremes()` for misaligned.
