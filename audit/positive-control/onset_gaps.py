"""NON-CONFIRMATORY AUDIT. Per episode: time from fault onset to the first planner decision, to the
first violation and to the PX4 failsafe, the health the first post-onset decision saw, and how many
planner decisions fit in the gap. Answers whether a CSA was physically possible.

    uv run python audit/positive-control/onset_gaps.py <mirror dir> [<mirror dir> ...] --out rows.json

A mirror dir is `breakdown.py mirror` output; only the counted attempt (the one with score.json) is used.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from lost_in_place.scoring import CSA_WINDOW_S, MIN_FLOW_QUALITY


def degraded(h: dict[str, Any] | None) -> bool:
    return bool(h) and (not h["xy_valid"] or h["flow_quality"] < MIN_FLOW_QUALITY)


def row(sweep: str, episode: dict[str, Any], score: dict[str, Any]) -> dict[str, Any]:
    events, decisions = episode["events"], episode["decisions"]
    onset = score.get("fault_onset_sim_s")
    anchor = next((e["fault_anchor_sim_s"] for e in events if "fault_anchor_sim_s" in e), None)
    loop_end = next((e["sim_s"] for e in events if "loop_end" in e), None)
    ref = onset if onset is not None else anchor
    out: dict[str, Any] = {
        "sweep": sweep,
        "scenario_id": score["scenario_id"],
        "state": score["scenario_id"].split("_", 1)[1],
        "planner": score["planner"],
        "observation": score["observation"],
        "seed": score["seed"],
        "outcome": score["outcome"],
        "onset_s": onset,
        "ref": "onset" if onset is not None else "fault_anchor",
        "csa": score.get("csa"),
        "first_post_onset_behaviour": score.get("first_post_onset_behaviour"),
        "violation_kind": score.get("violation_kind"),
    }
    if ref is None:
        return out

    def rel(t: float | None) -> float | None:
        return None if t is None else round(t - ref, 2)

    viol, fs = score.get("violation_sim_s"), score.get("failsafe_sim_s")
    out |= {
        "to_violation_s": rel(viol),
        "to_failsafe_s": rel(fs),
        "to_loop_end_s": rel(loop_end),
    }
    end = min((t for t in (viol, fs, loop_end) if t is not None), default=math.inf)
    planner = [d for d in decisions if not d.get("continued", False) and d["sim_us"] / 1e6 >= ref]
    continued = [d for d in decisions if d.get("continued", False) and d["sim_us"] / 1e6 >= ref]
    first = planner[0] if planner else None
    t_first = first["sim_us"] / 1e6 if first else None
    first_degraded = next((d for d in planner if degraded(d.get("health"))), None)
    out |= {
        "to_first_decision_s": rel(t_first),
        "first_decision_in_window": t_first is not None and t_first - ref <= CSA_WINDOW_S,
        "first_decision_before_end": t_first is not None and t_first < end,
        "first_decision_health": first.get("health") if first else None,
        "first_decision_degraded": degraded(first.get("health")) if first else None,
        "to_first_degraded_decision_s": rel(first_degraded["sim_us"] / 1e6) if first_degraded else None,
        "n_decisions_before_end": sum(d["sim_us"] / 1e6 < end for d in planner),
        "n_decisions_in_window_before_end": sum(
            d["sim_us"] / 1e6 < end and d["sim_us"] / 1e6 - ref <= CSA_WINDOW_S for d in planner
        ),
        "n_continued_before_end": sum(d["sim_us"] / 1e6 < end for d in continued),
        "decision_times_s": [rel(d["sim_us"] / 1e6) for d in planner[:8]],
        "decision_actions": [d["action"]["name"] for d in planner[:8]],
    }
    return out


def collect(mirror: Path) -> list[dict[str, Any]]:
    rows = []
    for score_path in sorted(mirror.rglob("score.json")):
        score = json.loads(score_path.read_text())
        episode = json.loads((score_path.parent / "episode.json").read_text())
        if "scenario_id" in score:
            rows.append(row(mirror.name, episode, score))
    return rows


def median(xs: list[float]) -> float | None:
    return round(statistics.median(xs), 2) if xs else None


def summarise(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cells: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        cells[(r["sweep"], r["planner"], r["state"], r["observation"])].append(r)
    out = []
    for (sweep, planner, state, obs), rs in sorted(cells.items()):
        n = len(rs)

        def vals(key: str, rs: list[dict[str, Any]] = rs) -> list[float]:
            return [r[key] for r in rs if r.get(key) is not None]

        out.append(
            {
                "sweep": sweep,
                "planner": planner,
                "state": state,
                "obs": obs,
                "n": n,
                "csa": f"{sum(bool(r['csa']) for r in rs)}/{sum(r['csa'] is not None for r in rs)}",
                "median_to_first_decision_s": median(vals("to_first_decision_s")),
                "max_to_first_decision_s": max(vals("to_first_decision_s"), default=None),
                "first_decision_in_window": sum(bool(r.get("first_decision_in_window")) for r in rs),
                "first_decision_before_end": sum(bool(r.get("first_decision_before_end")) for r in rs),
                "first_decision_degraded": sum(bool(r.get("first_decision_degraded")) for r in rs),
                "median_to_first_degraded_decision_s": median(vals("to_first_degraded_decision_s")),
                "violations": len(vals("to_violation_s")),
                "median_to_violation_s": median(vals("to_violation_s")),
                "min_to_violation_s": min(vals("to_violation_s"), default=None),
                "violation_before_first_decision": sum(
                    r.get("to_violation_s") is not None
                    and (
                        r.get("to_first_decision_s") is None
                        or r["to_violation_s"] <= r["to_first_decision_s"]
                    )
                    for r in rs
                ),
                "failsafes": len(vals("to_failsafe_s")),
                "median_to_failsafe_s": median(vals("to_failsafe_s")),
                "min_to_failsafe_s": min(vals("to_failsafe_s"), default=None),
                "median_decisions_before_end": median(vals("n_decisions_before_end")),
                "median_decisions_in_window_before_end": median(vals("n_decisions_in_window_before_end")),
                "zero_decisions_before_end": sum(r.get("n_decisions_before_end") == 0 for r in rs),
            }
        )
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("mirrors", nargs="+", type=Path)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    rows = [r for m in args.mirrors for r in collect(m)]
    summary = summarise(rows)
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))
    keys = list(summary[0])
    print("| " + " | ".join(keys) + " |")
    for s in summary:
        print("| " + " | ".join(str(s[k]) for k in keys) + " |")


if __name__ == "__main__":
    main()
