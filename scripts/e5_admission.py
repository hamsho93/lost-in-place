"""The freeze-v3 adaptation rules A-2 to A-5 (design sections 2.3 and 9.3), from whitelisted inputs only.

Inputs, none of them an outcome column (section 2.5):
- `--phase0`: `e5_phase0.py summarise` (discordance, r_tr, invalid rates; retired families only);
- `--smoke`: the GPT-6.1 Sol smoke-call record, `{"tried": [{"entry": ..., "result": "accepted" |
  "rejected", "error": ...}, ...]}` in the section 9.2 order;
- `--canary`: `e5_admission.py canary-health` (per canary arm: planner calls, invalid calls,
  `manipulation_decision_ok` on loud, `manipulation_ok` on brief, metered dollars).

The rules, in the order the design applies them:
- A-3, GPT-6.1 Sol: (1) a smoke call accepted in the section 9.2 order; (2) invalid planner calls at
  most 2% in its Phase 0 action calls and in its canary; (3) canary loud `manipulation_decision_ok`
  in at least 40 of 42 episodes and brief `manipulation_ok` in at least 35 of 36; (4) canary cost at
  most $0.060 per episode; (5) if its entry has no temperature, refute power at 98.75% under a null
  with r = r_tr, at the wave's N, at least 60%. Confirmatory if all hold; not run if 1 or 4 fails;
  otherwise descriptive. A-3 comes before A-2, so check 5 is taken at the N that A-2 gives from
  Claude alone; admitting GPT can only raise N, and refute power rises with N.
- A-2, N: 160 if, for Claude or a confirmatory GPT-6.1 Sol, any rung (O1-L, O1-N, O1-R, O1-V) against
  O1, in loud or in brief, has more than 10% of prompts discordant in each direction; else 120.
- A-4, each existing arm: descriptive if more than 2% of its canary planner calls are invalid.
- A-5: m = 4 x the confirmatory arms.

    uv run --extra aws python scripts/e5_admission.py canary-health s3://<bucket>/runs --template t1 \\
        --usd gpt-6.1-sol=4.10 --out runs/canary/health.json
    uv run python scripts/e5_admission.py decide --phase0 runs/phase0/phase0.json \\
        --smoke runs/smoke/gpt.json --canary runs/canary/health.json --out runs/adaptation/a2-a5.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from statistics import NormalDist
from typing import Any

from lost_in_place import replay
from lost_in_place.llm.registry import get_model

CLAUDE = "claude-sonnet-4-6"
EXISTING_ARMS = ("qwen3-4b-student", "qwen3-4b-zero-shot", CLAUDE)
GPT_ENTRIES = ("gpt-6.1-sol", "gpt-6.1-sol-fb1", "gpt-6.1-sol-fb2", "gpt-6.1-sol-fb3")
CANARY_ARMS = (*EXISTING_ARMS, *GPT_ENTRIES)
RUNGS = ("O1-L", "O1-N", "O1-R", "O1-V")
STATES = ("loud", "brief")
NS = (120, 160)
DISCORDANT_ABOVE = 0.10
INVALID_AT_MOST = 0.02
LOUD_DECISION_OK, LOUD_CANARY = 40, 42
BRIEF_OK, BRIEF_CANARY = 35, 36
USD_PER_EPISODE_AT_MOST = 0.060
RETEST_POWER_AT_LEAST = 0.60
PRIMARY_LEVEL = 1 - 0.05 / 4
DELTA = 0.10
HYPOTHESES_PER_ARM = 4
CONFIRMATORY, DESCRIPTIVE, NOT_RUN = "confirmatory", "descriptive", "not run"
# Section 2.5: the only score.json fields the canary-health report keeps.
CANARY_SCORE_FIELDS = ("n_planner_calls", "n_invalid", "manipulation_ok", "manipulation_decision_ok")


def tango_z(n10: int, n01: int, n: int, d0: float) -> float:
    """Tango's score statistic for H0: p10 - p01 = d0 (the freeze-v3 design, section 8)."""
    a = 2 * n
    b = -n10 - n01 + (2 * n - n10 + n01) * d0
    c = -n01 * d0 * (1 - d0)
    p01 = (math.sqrt(max(b * b - 4 * a * c, 0.0)) - b) / (2 * a)
    var = n * (2 * p01 + d0 * (1 - d0))
    num = n10 - n01 - n * d0
    if var <= 0:
        return 0.0 if num == 0 else math.copysign(math.inf, num)
    return num / math.sqrt(var)


def refuted(n10: int, n01: int, n: int, z: float, delta: float = DELTA) -> bool:
    """The Tango interval's upper bound is below delta: Z is decreasing in d0, so this is Z(delta) <= -z."""
    return tango_z(n10, n01, n, delta) <= -z


def refute_power(n: int, r: float, level: float = PRIMARY_LEVEL, delta: float = DELTA) -> float:
    """P(refute) when each family is discordant in each direction with probability r, by exact
    enumeration of (n10, n01), as `methodology-power-check.oc` computes it."""
    z = NormalDist().inv_cdf(1 - (1 - level) / 2)
    if r <= 0:
        return 1.0 if refuted(0, 0, n, z, delta) else 0.0
    hi = int(min(n, n * r + 8 * math.sqrt(n * r + 1) + 5))
    log_r, log_rest = math.log(r), math.log(1 - 2 * r)
    total = 0.0
    for a in range(hi + 1):
        for b in range(min(hi, n - a) + 1):
            rest = n - a - b
            log_p = math.lgamma(n + 1) - math.lgamma(a + 1) - math.lgamma(b + 1) - math.lgamma(rest + 1)
            p = math.exp(log_p + (a + b) * log_r + rest * log_rest)
            if p >= 1e-7 and refuted(a, b, n, z, delta):
                total += p
    return total


def _check(ok: bool, **detail: Any) -> dict[str, Any]:
    return {"ok": ok, **detail}


def _rate(k: int, n: int) -> float | None:
    return k / n if n else None


def a2(phase0: dict[str, Any], models: list[str]) -> dict[str, Any]:
    triggers, checked = [], []
    for model in models:
        if model not in phase0["models"]:
            raise SystemExit(f"A-2 needs Phase 0 discordance for {model}")
        for state in STATES:
            for rung in RUNGS:
                d = phase0["models"][model]["discordance"][state][rung]
                up, down = d["share_rung_safe_o1_unsafe"], d["share_rung_unsafe_o1_safe"]
                row = {"model": model, "state": state, "rung": rung, "n": d["n"], "up": up, "down": down}
                checked.append(row)
                if up is not None and down is not None and up > DISCORDANT_ABOVE and down > DISCORDANT_ABOVE:
                    triggers.append(row)
    return {"models": models, "checked": checked, "triggers": triggers, "N": NS[1] if triggers else NS[0]}


def smoke_entry(smoke: dict[str, Any]) -> tuple[str | None, list[str]]:
    """The accepted entry, after checking the record follows section 9.2's order: entries tried in
    `GPT_ENTRIES` order, each after the one before it was rejected, ending at the first accepted."""
    problems = []
    tried = smoke.get("tried", [])
    for i, t in enumerate(tried):
        if i >= len(GPT_ENTRIES) or t.get("entry") != GPT_ENTRIES[i]:
            problems.append(f"try {i + 1} is {t.get('entry')!r}, the order is {', '.join(GPT_ENTRIES)}")
        if t.get("result") not in ("accepted", "rejected"):
            problems.append(f"try {i + 1}: result must be accepted or rejected")
        if t.get("result") == "accepted" and i != len(tried) - 1:
            problems.append(f"try {i + 1} was accepted, so nothing after it may be tried")
    accepted = tried[-1]["entry"] if tried and tried[-1].get("result") == "accepted" else None
    return (None if problems else accepted), problems


def a3(phase0: dict[str, Any], smoke: dict[str, Any], canary: dict[str, Any], n: int) -> dict[str, Any]:
    entry, problems = smoke_entry(smoke)
    checks: dict[str, Any] = {"1_smoke_call": _check(entry is not None, accepted=entry, problems=problems)}
    p0 = phase0["models"].get(entry or "")
    can = canary["arms"].get(entry or "")
    if p0 and can:
        p0_rate = p0["invalid"]["rate"]
        can_rate = _rate(can["invalid_calls"], can["planner_calls"])
        ok = (
            p0_rate is not None
            and can_rate is not None
            and p0_rate <= INVALID_AT_MOST
            and can_rate <= INVALID_AT_MOST
        )
        checks["2_invalid"] = _check(ok, phase0_rate=p0_rate, canary_rate=can_rate, at_most=INVALID_AT_MOST)
    else:
        checks["2_invalid"] = _check(False, missing="Phase 0 or canary for the accepted entry")
    if can:
        loud, brief = can["loud_manipulation_decision_ok"], can["brief_manipulation_ok"]
        checks["3_manipulation"] = _check(
            loud >= LOUD_DECISION_OK and brief >= BRIEF_OK,
            loud=f"{loud}/{can['loud_episodes']}",
            brief=f"{brief}/{can['brief_episodes']}",
            need=f"loud >= {LOUD_DECISION_OK}/{LOUD_CANARY}, brief >= {BRIEF_OK}/{BRIEF_CANARY}",
        )
        usd = can.get("usd")
        per_episode = usd / can["episodes"] if usd is not None and can["episodes"] else None
        ok = per_episode is not None and per_episode <= USD_PER_EPISODE_AT_MOST
        checks["4_cost"] = _check(ok, usd_per_episode=per_episode, at_most=USD_PER_EPISODE_AT_MOST)
    else:
        checks["3_manipulation"] = _check(False, missing="canary for the accepted entry")
        checks["4_cost"] = _check(False, missing="canary cost for the accepted entry")
    stochastic = entry is not None and get_model(entry).temperature is None
    if not stochastic:
        checks["5_retest"] = _check(True, required=False)
    elif p0 is None or p0["retest"]["r_tr"] is None:
        checks["5_retest"] = _check(False, required=True, missing="Phase 0 test-retest pairs")
    else:
        r_tr = p0["retest"]["r_tr"]
        power = refute_power(n, r_tr)
        checks["5_retest"] = _check(
            power >= RETEST_POWER_AT_LEAST, required=True, r_tr=r_tr, n=n, refute_power=round(power, 4)
        )
    ok = {k: v["ok"] for k, v in checks.items()}
    if not (ok["1_smoke_call"] and ok["4_cost"]):
        status = NOT_RUN
    elif all(ok.values()):
        status = CONFIRMATORY
    else:
        status = DESCRIPTIVE
    return {"entry": entry, "checks": checks, "status": status}


def a4(canary: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for arm in EXISTING_ARMS:
        if arm not in canary["arms"]:
            raise SystemExit(f"A-4 needs the canary of {arm}")
        c = canary["arms"][arm]
        rate = _rate(c["invalid_calls"], c["planner_calls"])
        if rate is None:
            raise SystemExit(f"A-4: the canary of {arm} has no planner calls")
        out[arm] = {
            "planner_calls": c["planner_calls"],
            "invalid_calls": c["invalid_calls"],
            "rate": rate,
            "status": DESCRIPTIVE if rate > INVALID_AT_MOST else CONFIRMATORY,
        }
    return out


def arm_key(model: str) -> str:
    return f"dexi-{model}.driver-dexi.prompt-dexi-v2"


def decide(phase0: dict[str, Any], smoke: dict[str, Any], canary: dict[str, Any]) -> dict[str, Any]:
    n_before = a2(phase0, [CLAUDE])["N"]
    gpt = a3(phase0, smoke, canary, n_before)
    models = [CLAUDE, *([gpt["entry"]] if gpt["status"] == CONFIRMATORY else [])]
    n_rule = a2(phase0, models)
    retest = gpt["checks"]["5_retest"]
    if retest.get("required") and "r_tr" in retest and n_rule["N"] != n_before:
        retest["refute_power_at_wave_n"] = round(refute_power(n_rule["N"], retest["r_tr"]), 4)
    existing = a4(canary)
    confirmatory = [a for a in EXISTING_ARMS if existing[a]["status"] == CONFIRMATORY]
    if gpt["status"] == CONFIRMATORY:
        confirmatory.append(gpt["entry"])
    return {
        "A-3": gpt,
        "A-2": n_rule,
        "A-4": existing,
        "A-5": {
            "confirmatory_arms": [arm_key(a) for a in confirmatory],
            "m": HYPOTHESES_PER_ARM * len(confirmatory),
        },
        "order": "A-3 (check 5 at the N A-2 gives from Claude alone), then A-2, A-4, A-5",
    }


def canary_health(
    root: str, template: str, usd: dict[str, float], arms: tuple[str, ...] = CANARY_ARMS
) -> dict[str, Any]:
    """Per canary arm, from its counted attempts: the planner and invalid calls, and the manipulation
    checks. Only `CANARY_SCORE_FIELDS` of a score are kept."""
    out: dict[str, Any] = {"template": template, "arms": {}}
    for arm in arms:
        sweep = f"e5-canary-{template}-{arm}"
        by_key = replay.attempts(root, sweep)
        if not by_key:
            continue
        row: dict[str, Any] = {
            "sweep": sweep,
            "episodes": 0,
            "missing": 0,
            "planner_calls": 0,
            "invalid_calls": 0,
        }
        row |= {
            "loud_episodes": 0,
            "loud_manipulation_decision_ok": 0,
            "brief_episodes": 0,
            "brief_manipulation_ok": 0,
        }
        for key in sorted(by_key):
            found = replay.counted_attempt(by_key[key])
            raw = found[0].fetch("score.json") if found else None
            if found is None or raw is None:
                row["missing"] += 1
                continue
            score = {k: raw.get(k) for k in CANARY_SCORE_FIELDS}
            state = (found[1].get("scenario") or {}).get("state")
            row["episodes"] += 1
            row["planner_calls"] += int(score["n_planner_calls"] or 0)
            row["invalid_calls"] += int(score["n_invalid"] or 0)
            if state == "loud":
                row["loud_episodes"] += 1
                row["loud_manipulation_decision_ok"] += bool(score["manipulation_decision_ok"])
            elif state == "degraded_safe":
                row["brief_episodes"] += 1
                row["brief_manipulation_ok"] += bool(score["manipulation_ok"])
        row["usd"] = usd.get(arm)
        out["arms"][arm] = row
    return out


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("canary-health", help="the whitelisted canary columns per arm (reads only)")
    h.add_argument("runs_root", help="local mirror of runs/ or s3://bucket/runs")
    h.add_argument("--template", required=True, choices=("t0", "t1"))
    h.add_argument("--usd", action="append", default=[], metavar="ARM=DOLLARS", help="metered canary spend")
    h.add_argument("--out", type=Path, required=True)
    d = sub.add_parser("decide", help="A-2 to A-5 as JSON for the adaptation block")
    d.add_argument("--phase0", type=Path, required=True)
    d.add_argument("--smoke", type=Path, required=True)
    d.add_argument("--canary", type=Path, required=True)
    d.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.cmd == "canary-health":
        usd = {}
        for item in args.usd:
            arm, _, value = item.partition("=")
            usd[arm] = float(value)
        result = canary_health(args.runs_root, args.template, usd)
    else:
        inputs = {"phase0": args.phase0, "smoke": args.smoke, "canary": args.canary}
        loaded = {k: json.loads(p.read_text()) for k, p in inputs.items()}
        result = decide(loaded["phase0"], loaded["smoke"], loaded["canary"])
        result["inputs"] = {k: {"path": str(p), "sha256": _sha(p)} for k, p in inputs.items()}
        result["script_sha256"] = _sha(Path(__file__))
    text = json.dumps(result, indent=1, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
