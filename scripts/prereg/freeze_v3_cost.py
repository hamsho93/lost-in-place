"""Cost model for freeze-v3 (design §10): 20,000 draws per scenario, from measured E2/E4 rates and list
prices.

    uv run python scripts/prereg/freeze_v3_cost.py

Per LLM arm, 2,040 episodes at N = 120 and 2,560 at N = 160. The pre-freeze work (Phase 0, the wording
check and all-level canaries) is October's; the wave is November's. GPT-6.1 Sol runs after the three
existing arms, and Claude Sonnet 5.5 runs last. Each goes to December when the §10.4 month-fit sum fails,
and Sonnet 5.5's sum includes GPT's projection, so a deferred GPT defers Sonnet 5.5 too. Either arm is
not run when its canary $/episode exceeds the A-3 / A-3b cap. A dropped temperature (Phase 0 sent twice)
is not modelled.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field

CMU_MIN = 0.05718 * 2  # $ per copy-minute: 2 CMUs per copy, us-east-1 list price (OSS v1)
ADMIT = 0.060  # A-3 / A-3b: the canary's cost per episode must not exceed this
S9 = 400.0  # metered November hard stop; the AWS budget action fires at 450 actual
FIT = 1.15
IN_USD, OUT_USD = 2.2e-6, 11e-6  # GPT-6.1 Sol and Claude Sonnet 5.5 list prices per token
CLAUDE5_TOKENS = 1.3  # Claude 5.x counts about 30% more tokens for the same text
CANARY_EPISODES = 78
SONNET5_PRE_CALLS = 60 * 2 * 5 * 2 + 60 * 7 + 66 * 2 * 2 * 3  # Phase 0 action + probe, first step, wording

rng = random.Random(0)
rng5 = random.Random(1)


def ln(r: random.Random, med: float, sigma: float) -> float:
    return med * r.lognormvariate(0, sigma)


def per_arm(n: int) -> int:
    # loud 7 levels x n, brief 6 x n, healthy 6 x 60, silent 2 states x 2 levels x 30
    return 7 * n + 6 * n + 6 * 60 + 2 * 2 * 30


def gpt_rate(r_med: float) -> tuple[float, float]:
    calls = rng.uniform(5, 8)
    r = ln(rng, r_med, 0.8)
    return calls * (1500 * IN_USD + (50 + r) * OUT_USD) + rng.uniform(0.005, 0.008), r


def sonnet5_call(r: float) -> float:
    return CLAUDE5_TOKENS * 1500 * IN_USD + (CLAUDE5_TOKENS * 50 + r) * OUT_USD


def sonnet5_probe_call(r: float) -> float:
    return CLAUDE5_TOKENS * 1600 * IN_USD + (CLAUDE5_TOKENS * 30 + r) * OUT_USD


@dataclass
class Draw:
    pre: dict[str, float] = field(default_factory=dict)
    nov: dict[str, float] = field(default_factory=dict)
    dec: dict[str, float] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)
    rate: dict[str, float] = field(default_factory=dict)


def month_fit(name: str, d: Draw, cost: float, proj: float, admitted: bool, spent: float) -> float:
    """Place an arm by the §10.4 sum; return the projection the next arm's sum must include."""
    if not admitted:
        d.status[name] = "not admitted"
        return 0.0
    if spent + proj <= S9:
        d.status[name] = "november"
        d.nov[name] = cost * 1.01
        return proj
    d.status[name] = "december"
    d.dec[name] = cost * 1.01
    return proj


def draw(n: int = 120, r_med: float = 200, waste: bool = False, pre_in_nov: bool = False) -> Draw:
    d = Draw()
    d.pre["phase0"] = rng.uniform(8, 15) + rng.uniform(3, 5)  # replay + first-step prompts, Claude and GPT
    d.pre["wording"] = rng.uniform(5, 12)
    d.pre["canaries"] = rng.uniform(6, 10) + rng.uniform(4, 12)  # four arms at all levels, Qwen warm-ups

    e = per_arm(n)
    w = d.nov
    w["scripted"] = 2 * 6 * n * rng.uniform(0.004, 0.006)
    over_s = rng.uniform(10, 40) if not waste else rng.uniform(45, 120)
    over_z = rng.uniform(20, 60) if not waste else rng.uniform(150, 260)
    w["student"] = (e * 0.076 * rng.uniform(1.0, 1.8) * ln(rng, 1, 0.1) + over_s) * CMU_MIN + e * rng.uniform(
        0.004, 0.006
    )
    w["zero_shot"] = (e * 0.241 * 0.92 * ln(rng, 1.10, 0.18) + over_z) * CMU_MIN + e * rng.uniform(
        0.006, 0.009
    )
    w["claude"] = e * 0.0335 * ln(rng, 1, 0.12) + e * rng.uniform(0.004, 0.006)
    rate, r = gpt_rate(r_med)
    canary = rate * ln(rng, 1, 0.10)
    probe_calls = 7 * n
    w["probe"] = (
        probe_calls * (1600 * 3.3e-6 + 30 * 16.5e-6)
        + rng.uniform(10, 20) * CMU_MIN
        + rng.uniform(30, 70) * CMU_MIN
    )
    w["labeller"] = rng.uniform(21, 32) * (n / 120)
    w["other_aws"] = rng.uniform(3, 10)
    w["retries"] = 0.01 * (w["student"] + w["zero_shot"] + w["claude"] + w["scripted"])

    calls5 = rng5.uniform(5, 7)
    r5 = ln(rng5, 200, 0.8)
    rate5 = calls5 * sonnet5_call(r5) + rng5.uniform(0.005, 0.008)
    canary5 = rate5 * ln(rng5, 1, 0.10)
    d.rate["sonnet5"] = calls5 * sonnet5_call(r5)
    d.pre["sonnet5"] = SONNET5_PRE_CALLS * sonnet5_call(r5) + CANARY_EPISODES * canary5

    spent = sum(w.values()) + (sum(d.pre.values()) if pre_in_nov else 0.0)
    gpt_probe = probe_calls * (1600 * IN_USD + (30 + r) * OUT_USD)
    gpt_proj = month_fit(
        "gpt", d, e * rate + gpt_probe, FIT * (e * canary + gpt_probe), canary <= ADMIT, spent
    )
    if d.status["gpt"] == "december":
        spent = S9
    probe5 = probe_calls * sonnet5_probe_call(r5)
    month_fit(
        "sonnet5", d, e * rate5 + probe5, FIT * (e * canary5 + probe5), canary5 <= ADMIT, spent + gpt_proj
    )
    return d


def q(a: Sequence[float], p: float) -> float:
    s = sorted(a)
    return s[int(p * (len(s) - 1))]


def summarise(
    label: str, n: int = 120, r_med: float = 200, waste: bool = False, pre_in_nov: bool = False
) -> None:
    runs = [draw(n, r_med, waste, pre_in_nov) for _ in range(20000)]
    pre = [sum(d.pre.values()) for d in runs]
    nov = [sum(d.nov.values()) + (sum(d.pre.values()) if pre_in_nov else 0.0) for d in runs]
    dec = [sum(d.dec.values()) for d in runs if d.dec]
    print(f"\n== {label}")
    for k in ("scripted", "student", "zero_shot", "claude", "probe", "labeller", "other_aws"):
        a = [d.nov[k] for d in runs]
        print(f"  {k:12} median {q(a, 0.5):6.1f}  p90 {q(a, 0.9):6.1f}")
    for k in ("gpt", "sonnet5"):
        a = [{**d.nov, **d.dec}[k] for d in runs if k in d.nov or k in d.dec]
        print(f"  {k} (when run, either month) median {q(a, 0.5):6.1f}  p90 {q(a, 0.9):6.1f}")
    a = [d.rate["sonnet5"] for d in runs]
    print(f"  sonnet5 inference $/episode median {q(a, 0.5):.4f}  p90 {q(a, 0.9):.4f}")
    s5 = [d.pre["sonnet5"] for d in runs]
    print(f"  pre-freeze        median {q(pre, 0.5):.0f}  p90 {q(pre, 0.9):.0f}", end="")
    print(f"  (Sonnet 5.5 part median {q(s5, 0.5):.0f}  p90 {q(s5, 0.9):.0f})")
    print(f"  November total    median {q(nov, 0.5):.0f}  p90 {q(nov, 0.9):.0f}", end="")
    print(f"  p99 {q(nov, 0.99):.0f}  max {max(nov):.0f}")
    if dec:
        print(f"  December (when any) median {q(dec, 0.5):.0f}  p90 {q(dec, 0.9):.0f}  max {max(dec):.0f}")
    for k, name in (("gpt", "GPT"), ("sonnet5", "Sonnet 5.5")):
        states = ("november", "december", "not admitted")
        shares = {s: sum(d.status[k] == s for d in runs) / len(runs) for s in states}
        print(f"  {name:10} " + "  ".join(f"{s} {v:.0%}" for s, v in shares.items()))


def main() -> None:
    summarise("N=120, GPT effort low (R median 200)")
    summarise("N=120, GPT at provider default (R median 300)", r_med=300)
    summarise("N=120, E2-style warm-copy waste", waste=True)
    summarise("N=120, pre-freeze work slips into November", pre_in_nov=True)
    summarise("N=160", n=160)
    summarise("N=160, E2-style warm-copy waste", n=160, waste=True)


if __name__ == "__main__":
    main()
