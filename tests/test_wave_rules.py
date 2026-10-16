"""Stop rules S-2 to S-9 and the section 10.4 month-fit sum, as pure functions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from lost_in_place.wave import rules
from lost_in_place.wave.config import Arm, Month, Rules
from lost_in_place.wave.episodes import AttemptView

R = Rules()
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def view(kind: str | None = None, calls: int = 10, invalid: int = 0) -> AttemptView:
    return AttemptView(
        "k", "l", 0.0, kind, kind is None, {"n_planner_calls": calls, "n_invalid": invalid}, 0, 0, None
    )


def test_s2_harness_rate_and_consecutive_link_failures() -> None:
    assert rules.s2_harness([view()] * 97 + [view("timeout")] * 3, R) is None
    assert "4 harness failures" in str(rules.s2_harness([view("timeout")] * 4 + [view()] * 96, R))
    spread = ([view("timeout")] + [view()] * 33) * 4
    assert rules.s2_harness(spread, R) is None
    links = [view(), view("no_local_position"), view("error: LinkLost"), view("no_local_position")]
    assert "consecutive" in str(rules.s2_harness(links, R))
    assert rules.s2_harness([view("no_local_position"), view(), view("error: LinkLost")], R) is None


def test_s3_invalid_rate_checked_on_the_first_100() -> None:
    assert rules.s3_invalid([view(invalid=1)] * 99, R) is None
    assert rules.s3_invalid([view(calls=10, invalid=0)] * 98 + [view(calls=10, invalid=10)] * 2, R) is None
    assert "S-3" in str(rules.s3_invalid([view(calls=10, invalid=1)] * 100, R))


def test_s4_throttle_rate() -> None:
    assert not rules.s4_throttled(0, 0, R)
    assert not rules.s4_throttled(10, 990, R)
    assert rules.s4_throttled(11, 989, R)


def test_s5_warm_up() -> None:
    assert rules.s5_warm(None, NOW, R) == "cold"
    started = (NOW - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert rules.s5_warm({"started_at": started}, NOW, R) == "warming"
    assert rules.s5_warm({"started_at": started, "first_success_at": started}, NOW, R) == "warm"
    assert rules.s5_warm({"started_at": started}, NOW + timedelta(minutes=21), R) == "failed"
    assert rules.s5_warm({"started_at": started, "failed_at": started}, NOW, R) == "failed"


def test_s6_idle_copy() -> None:
    up = [(NOW - timedelta(minutes=m), 1.0) for m in range(12)]
    assert rules.s6_idle(up, NOW - timedelta(minutes=11), NOW, R)
    assert not rules.s6_idle(up, NOW - timedelta(minutes=5), NOW, R)
    assert not rules.s6_idle(up, None, NOW, R)
    down = [*up[:3], (NOW - timedelta(minutes=4), 0.0), *up[5:]]
    assert not rules.s6_idle(down, NOW - timedelta(minutes=11), NOW, R)


def test_s7_schedule_and_projection() -> None:
    assert not rules.s7_due(99, 0, R)
    assert rules.s7_due(100, 0, R)
    assert not rules.s7_due(349, 100, R)
    assert rules.s7_due(350, 100, R)
    fits = [("gpt", 90.0), ("sonnet", 60.0)]
    assert rules.s7_projection("claude", 10, 100, 1000, 120, 0, fits, 400).verdict == "continue"
    over = rules.s7_projection("claude", 13, 100, 1000, 120, 250, fits, 400)
    assert over.verdict == "continue" and "over its $120.00 cap" in over.reason
    sonnet_later = rules.s7_projection("claude", 13, 100, 1000, 120, 300, fits, 400)
    assert sonnet_later.verdict == "defer" and sonnet_later.deferred == ("sonnet",)
    both_later = rules.s7_projection("claude", 13, 100, 1000, 120, 320, fits, 400)
    assert both_later.verdict == "defer" and both_later.deferred == ("gpt", "sonnet")
    assert rules.s7_projection("claude", 13, 100, 1000, 120, 410, fits, 400).verdict == "pause"


def test_s8_admission_and_rolling_cost() -> None:
    gpt = Arm(name="gpt", sweep="s.yaml", cap=40, month_fit=True, canary_usd_per_episode=0.05)
    assert rules.s8_admitted(gpt, R) is None
    assert "exceeds" in str(rules.s8_admitted(gpt.model_copy(update={"canary_usd_per_episode": 0.07}), R))
    assert "no canary" in str(rules.s8_admitted(gpt.model_copy(update={"canary_usd_per_episode": None}), R))
    assert rules.s8_rolling([0.07] * 99, 0.05, R) is None
    assert rules.s8_rolling([0.074] * 100, 0.05, R) is None
    assert "S-8" in str(rules.s8_rolling([0.01] * 50 + [0.076] * 100, 0.05, R))


def test_s9_stop_and_the_october_p90_rule() -> None:
    months = {
        "2026-10": Month(stop_usd=420, budget_actual=True, job_p90_check=True),
        "2026-11": Month(stop_usd=400),
    }
    assert rules.s9_stop("2026-11", months, 400.0) is None
    assert "over the $400 stop" in str(rules.s9_stop("2026-11", months, 400.01))
    assert rules.s9_stop("2026-12", months, 1e6) is None
    assert "no approved budget" in str(rules.s9_budget("2026-12", months))
    unset = {**months, "2026-12": Month(arms=("gpt",))}
    assert rules.s9_stop("2026-12", unset, 1e6) is None
    assert "no approved budget" in str(rules.s9_budget("2026-12", unset))
    assert "no approved budget" in str(rules.s9_job_fits("2026-12", unset, 0, 0))
    assert rules.s9_budget("2026-11", months) is None
    assert rules.s9_job_fits("2026-10", months, 330, 90) is None
    assert "exceeds $420" in str(rules.s9_job_fits("2026-10", months, 330.01, 90))
    assert rules.s9_job_fits("2026-11", months, 399, 90) is None


def fit(*arms: rules.FitArm, committed: float = 250, stop: float | None = 400, **kw: Any) -> dict[str, str]:
    kw.setdefault("next_month_budget", None)
    kw.setdefault("deadline_passed", False)
    return {k: v.decision for k, v in rules.month_fit_sequence(committed, arms, stop, **kw).items()}


GPT = rules.FitArm("gpt", rules.fit_projection(2040, 0.040, 5, R), next_month=True)
SONNET = rules.FitArm("sonnet", rules.fit_projection(2040, 0.045, 5, R), next_month=True)


def test_month_fit_single_arm_run_defer_drop() -> None:
    one = rules.month_fit_sequence(200 + 50, [GPT], 400, next_month_budget=None, deadline_passed=False)
    assert one["gpt"].decision == "run" and round(one["gpt"].arm_usd, 2) == round(1.15 * (81.6 + 5), 2)
    assert fit(GPT, committed=350, next_month_budget=150) == {"gpt": "defer"}
    assert fit(GPT, committed=350) == {"gpt": "defer"}
    assert fit(GPT, committed=350, deadline_passed=True) == {"gpt": "drop"}
    assert fit(GPT, committed=350, next_month_budget=50, deadline_passed=True) == {"gpt": "drop"}


def test_month_fit_sums_sonnet_after_gpt() -> None:
    assert fit(GPT, SONNET, committed=150) == {"gpt": "run", "sonnet": "run"}
    results = rules.month_fit_sequence(250, [GPT, SONNET], 400, next_month_budget=370, deadline_passed=False)
    assert results["gpt"].decision == "run" and results["sonnet"].decision == "defer"
    assert round(results["sonnet"].total_usd, 2) == round(250 + GPT.projection_usd + SONNET.projection_usd, 2)
    assert fit(GPT, SONNET, committed=250, next_month_budget=None, deadline_passed=True) == {
        "gpt": "run",
        "sonnet": "drop",
    }


def test_month_fit_keeps_the_order_and_shares_next_month() -> None:
    assert fit(GPT, SONNET, committed=320, next_month_budget=370) == {"gpt": "defer", "sonnet": "defer"}
    cheap = rules.FitArm("sonnet", 10.0, next_month=True)
    assert fit(GPT, cheap, committed=320, next_month_budget=370) == {"gpt": "defer", "sonnet": "defer"}
    only_gpt = GPT.projection_usd + 1
    assert fit(GPT, SONNET, committed=320, next_month_budget=only_gpt, deadline_passed=True) == {
        "gpt": "defer",
        "sonnet": "drop",
    }
    not_listed = rules.FitArm("sonnet", SONNET.projection_usd)
    assert fit(GPT, not_listed, committed=320, next_month_budget=370, deadline_passed=True) == {
        "gpt": "defer",
        "sonnet": "drop",
    }


def test_month_fit_counts_a_started_arm_and_skips_none() -> None:
    running = rules.FitArm("gpt", 40.0, started=True)
    assert fit(running, SONNET, committed=330, next_month_budget=370) == {"gpt": "run", "sonnet": "defer"}
    assert fit(running, SONNET, committed=200) == {"gpt": "run", "sonnet": "run"}
    assert fit(SONNET, committed=250) == {"sonnet": "run"}


def test_month_fit_in_a_month_without_a_stop() -> None:
    assert fit(GPT, SONNET, committed=0, stop=None, deadline_passed=True) == {"gpt": "drop", "sonnet": "drop"}
    assert fit(GPT, committed=0, stop=None) == {"gpt": "defer"}
