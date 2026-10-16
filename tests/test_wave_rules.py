"""Stop rules S-2 to S-9 and the section 10.4 month-fit sum, as pure functions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    assert rules.s7_projection("claude", 10, 100, 1000, 120, 0, 0, 400).verdict == "continue"
    over = rules.s7_projection("claude", 13, 100, 1000, 120, 390, 300, 400)
    assert over.verdict == "continue" and "over its $120.00 cap" in over.reason
    assert rules.s7_projection("claude", 13, 100, 1000, 120, 450, 390, 400).verdict == "continue-gpt-december"
    assert rules.s7_projection("claude", 13, 100, 1000, 120, 450, 410, 400).verdict == "pause"


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
    assert "no approved budget" in str(rules.s9_stop("2026-12", months, 0))
    assert rules.s9_job_fits("2026-10", months, 330, 90) is None
    assert "exceeds $420" in str(rules.s9_job_fits("2026-10", months, 330.01, 90))
    assert rules.s9_job_fits("2026-11", months, 399, 90) is None


def test_month_fit_run_defer_drop() -> None:
    fit = rules.month_fit(200, 50, 1000, 0.1, 5, R, 400, next_month_budget=None, deadline_passed=False)
    assert fit.decision == "run" and round(fit.gpt_usd, 2) == round(1.15 * 105, 2)
    defer = rules.month_fit(300, 50, 1000, 0.1, 5, R, 400, next_month_budget=150, deadline_passed=False)
    assert defer.decision == "defer" and "next month's budget" in defer.reason
    wait = rules.month_fit(300, 50, 1000, 0.1, 5, R, 400, next_month_budget=None, deadline_passed=False)
    assert wait.decision == "defer"
    drop = rules.month_fit(300, 50, 1000, 0.1, 5, R, 400, next_month_budget=None, deadline_passed=True)
    assert drop.decision == "drop"
