"""Cost model for freeze-v3 (design §10): 20,000 draws per scenario, from measured E2/E4 rates and list
prices.

    uv run python scripts/prereg/freeze_v3_cost.py

Per LLM arm, 2,040 episodes at N = 120 and 2,560 at N = 160. The pre-freeze work (the frontier smoke,
Phase 0, the wording check and all-level canaries) is October's; the wave is November's. GPT-5.6 Sol runs
after the three existing arms, and Claude Sonnet 5.5 runs last. Each goes to December when the §10.4
month-fit sum fails. Sonnet 5.5's sum is taken after GPT has settled, so it holds GPT's metered cost, and a
deferred GPT defers Sonnet 5.5 too. Either arm is not run when its canary $/episode, compute included,
exceeds the A-3 / A-3b cap.

GPT-5.6 Sol's tokens are the frontier smoke's (Oct 16-17, O1, `-fb2` at effort `low`): about 590 input
and 80 output tokens per call, reasoning included. Its rungs and brief are unmeasured, so the output
tokens keep a wide spread and the calls per episode stay at 5-8 (the smoke's O1 mix averaged 4.6).

Phase 0 uses `scripts/e5_phase0.py estimate`'s figures at the median token assumptions, scaled by each
draw's output tokens: Sonnet 5.5 is costed as its `-fb1` entry (action calls twice), and GPT as `-fb2`
(action calls twice), because the smoke found that it rejects temperature.

The `wp8` Sonnet 5.5 variant reproduces the WP8 submitter's check (a lognormal $/episode with median
$0.045 and P90 $0.065, and a canary test without compute), so the two estimates can be compared.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field

CMU_MIN = 0.05718 * 2  # $ per copy-minute: 2 CMUs per copy, us-east-1 list price (OSS v1)
ADMIT = 0.060  # A-3 / A-3b: the canary's cost per episode must not exceed this
S9 = 400.0  # metered November hard stop; the AWS budget action fires at 450 actual
FIT = 1.15
IN_USD, OUT_USD = 2.2e-6, 11e-6  # Claude Sonnet 5.5 list prices per token (us. profile)
GPT_IN_USD, GPT_OUT_USD = 4.4e-6, 22e-6  # GPT-5.6 Sol list prices per token (us. profile)
GPT_IN_TOKENS = 590  # per call, measured
GPT_R_LOW = 80  # output tokens per call at effort low, reasoning included: the median, measured
GPT_R_DEFAULT = 95  # the same at the provider default (medium)
CLAUDE5_TOKENS = 1.3  # Claude 5.x counts about 30% more tokens for the same text
CANARY_EPISODES = 78
PHASE0_CLAUDE = 11.50  # 2,820 calls (scripts/e5_phase0.py estimate)
PHASE0_GPT = 9.05  # -fb2: 2,220 calls, the action calls twice
PHASE0_SONNET5 = 14.04  # -fb1: 2,220 calls
WORDING_SONNET5 = 2.81  # A-1 probe: 792 calls
SMOKE_USD = 12.3  # the frontier smokes of Oct 16-17 (Sonnet 5.5 and GPT-5.6 Sol), as metered
SONNET5_WP8 = (0.045, 0.287)  # lognormal median and sigma: P90 $0.065

rng = random.Random(0)
rng5 = random.Random(1)
rngp = random.Random(2)


def ln(r: random.Random, med: float, sigma: float) -> float:
    return med * r.lognormvariate(0, sigma)


def per_arm(n: int) -> int:
    # loud 7 levels x n, brief 6 x n, healthy 6 x 60, silent 2 states x 2 levels x 30
    return 7 * n + 6 * n + 6 * 60 + 2 * 2 * 30


def gpt_call(r: float) -> float:
    return GPT_IN_TOKENS * GPT_IN_USD + r * GPT_OUT_USD


def gpt_rate(r_med: float) -> tuple[float, float]:
    calls = rng.uniform(5, 8)
    r = ln(rng, r_med, 0.8)
    return calls * gpt_call(r) + rng.uniform(0.005, 0.008), r


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
    need: dict[str, float] = field(default_factory=dict)


def place(name: str, d: Draw, cost: float, proj: float, admitted: bool, spent: float, defer: bool) -> None:
    """Place a late arm by the §10.4 sum: November if it fits under S-9, else December."""
    if not admitted:
        d.status[name] = "not admitted"
    elif not defer and spent + proj <= S9:
        d.status[name] = "november"
        d.nov[name] = cost * 1.01
    else:
        d.status[name] = "december"
        d.dec[name] = cost * 1.01
        d.need[name] = proj


def draw(
    n: int = 120,
    r_med: float = GPT_R_LOW,
    waste: bool = False,
    pre_in_nov: bool = False,
    sonnet: str = "model",
) -> Draw:
    d = Draw()
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
    compute5 = rng5.uniform(0.005, 0.008)
    noise5 = ln(rng5, 1, 0.10)
    inference5 = calls5 * sonnet5_call(r5)
    if sonnet == "wp8":
        inference5 = ln(rng5, *SONNET5_WP8)
    rate5 = inference5 + compute5
    canary5 = (inference5 if sonnet in ("wp8", "no-compute") else rate5) * noise5
    d.rate["sonnet5"] = inference5

    gpt_factor = gpt_call(r) / gpt_call(GPT_R_LOW)
    s5_factor = sonnet5_call(r5) / sonnet5_call(200)
    d.pre["smoke"] = SMOKE_USD
    d.pre["phase0_claude"] = PHASE0_CLAUDE * ln(rngp, 1, 0.12)
    d.pre["phase0_gpt"] = PHASE0_GPT * gpt_factor
    d.pre["phase0_sonnet5"] = PHASE0_SONNET5 * s5_factor
    d.pre["wording"] = rngp.uniform(5, 12) + WORDING_SONNET5 * s5_factor  # dry run, Claude and GPT probes
    d.pre["canaries"] = rngp.uniform(6, 10) + rngp.uniform(4, 12)  # four arms at all levels, Qwen warm-ups
    d.pre["canary_sonnet5"] = CANARY_EPISODES * rate5 * noise5

    spent = sum(w.values()) + (sum(d.pre.values()) if pre_in_nov else 0.0)
    gpt_probe = probe_calls * (1600 * GPT_IN_USD + r * GPT_OUT_USD)
    place("gpt", d, e * rate + gpt_probe, FIT * (e * canary + gpt_probe), canary <= ADMIT, spent, False)
    probe5 = probe_calls * sonnet5_probe_call(r5)
    place(
        "sonnet5",
        d,
        e * rate5 + probe5,
        FIT * (e * canary5 + probe5),
        canary5 <= ADMIT,
        spent + d.nov.get("gpt", 0.0),
        d.status["gpt"] == "december",
    )
    return d


def q(a: Sequence[float], p: float) -> float:
    s = sorted(a)
    return s[int(p * (len(s) - 1))]


def summarise(
    label: str,
    n: int = 120,
    r_med: float = GPT_R_LOW,
    waste: bool = False,
    pre_in_nov: bool = False,
    sonnet: str = "model",
) -> None:
    runs = [draw(n, r_med, waste, pre_in_nov, sonnet) for _ in range(20000)]
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
    print(f"  pre-freeze        median {q(pre, 0.5):.1f}  p90 {q(pre, 0.9):.1f}")
    for k in d_keys(runs):
        a = [d.pre[k] for d in runs]
        print(f"    {k:15} median {q(a, 0.5):6.2f}  p90 {q(a, 0.9):6.2f}")
    p0 = [d.pre["phase0_claude"] + d.pre["phase0_gpt"] + d.pre["phase0_sonnet5"] for d in runs]
    print(f"    {'phase0 total':15} median {q(p0, 0.5):6.2f}  p90 {q(p0, 0.9):6.2f}")
    print(f"  November total    median {q(nov, 0.5):.0f}  p90 {q(nov, 0.9):.0f}", end="")
    print(f"  p99 {q(nov, 0.99):.0f}  max {max(nov):.0f}")
    if dec:
        need = [sum(d.need.values()) for d in runs if d.need]
        print(
            f"  December (when any) median {q(dec, 0.5):.0f}  p90 {q(dec, 0.9):.0f}  max {max(dec):.0f}",
            end="",
        )
        print(f"; 1.15x need median {q(need, 0.5):.0f}  p90 {q(need, 0.9):.0f}  max {max(need):.0f}")
    for k, name in (("gpt", "GPT"), ("sonnet5", "Sonnet 5.5")):
        states = ("november", "december", "not admitted")
        shares = {s: sum(d.status[k] == s for d in runs) / len(runs) for s in states}
        print(f"  {name:10} " + "  ".join(f"{s} {v:.0%}" for s, v in shares.items()))


def d_keys(runs: Sequence[Draw]) -> list[str]:
    return list(runs[0].pre)


def main() -> None:
    summarise(f"N=120, GPT effort low (output median {GPT_R_LOW}, measured)")
    summarise(
        f"N=120, GPT at provider default (output median {GPT_R_DEFAULT}, measured)", r_med=GPT_R_DEFAULT
    )
    summarise("N=120, E2-style warm-copy waste", waste=True)
    summarise("N=120, pre-freeze work slips into November", pre_in_nov=True)
    summarise("N=160", n=160)
    summarise("N=160, E2-style warm-copy waste", n=160, waste=True)
    summarise("N=120, Sonnet 5.5 canary test without compute", sonnet="no-compute")
    summarise("N=120, Sonnet 5.5 as the WP8 submitter's model", sonnet="wp8")
    summarise("N=160, Sonnet 5.5 as the WP8 submitter's model", n=160, sonnet="wp8")


if __name__ == "__main__":
    main()
