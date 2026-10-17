"""Stop rules S-0 to S-9 and the section 10.4 month-fit sum, as pure functions of harness and cost
signals. None of them reads an outcome column: they see attempt views (`episodes.AttemptView`),
metered dollars and CloudWatch counts only. The submitter (`wave.submit`) gathers the inputs and acts
on the verdicts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from lost_in_place.wave.config import Arm, Month, Rules
from lost_in_place.wave.episodes import LINK_FAILURE_KINDS, AttemptView
from lost_in_place.wave.ledger import parse_iso


def s2_harness(attempts: Sequence[AttemptView], rules: Rules) -> str | None:
    """More than 3% harness failures in any rolling 100 attempts, or 3 consecutive link failures."""
    window, limit = rules.harness_window, rules.harness_max_rate * rules.harness_window
    flags = [a.harness_failure for a in attempts]
    for end in range(1, len(flags) + 1):
        failures = sum(flags[max(0, end - window) : end])
        if failures > limit:
            return f"S-2: {failures} harness failures in {min(end, window)} consecutive attempts"
    run = 0
    for a in attempts:
        run = run + 1 if a.failure_kind in LINK_FAILURE_KINDS else 0
        if run >= rules.consecutive_link_failures:
            return f"S-2: {run} consecutive no_local_position / LinkLost attempts"
    return None


def s3_invalid(results: Sequence[AttemptView], rules: Rules) -> str | None:
    """Invalid planner replies above 2% of calls in the arm's first 100 episodes (checked at 100)."""
    if len(results) < rules.invalid_window:
        return None
    first = results[: rules.invalid_window]
    calls = sum(int(r.fields.get("n_planner_calls") or 0) for r in first)
    invalid = sum(int(r.fields.get("n_invalid") or 0) for r in first)
    if calls and invalid / calls > rules.invalid_max_rate:
        return f"S-3: {invalid}/{calls} invalid planner replies in the first {len(first)} episodes"
    return None


def s4_throttled(throttles: float, invocations: float, rules: Rules) -> bool:
    calls = throttles + invocations
    return calls > 0 and throttles / calls > rules.throttle_max_rate


WarmState = Literal["warm", "warming", "failed", "cold"]


def s5_warm(record: dict[str, Any] | None, now: datetime, rules: Rules) -> WarmState:
    """Never submit an import's arm before a first successful call; give up after 30 min of warm-up."""
    if not record:
        return "cold"
    if record.get("first_success_at"):
        return "warm"
    if record.get("failed_at"):
        return "failed"
    started = record.get("started_at")
    if started and now - parse_iso(started) > timedelta(seconds=rules.warm_timeout_s):
        return "failed"
    return "warming"


def s6_idle(
    copy_points: Sequence[tuple[datetime, float]], idle_since: datetime | None, now: datetime, rules: Rules
) -> bool:
    """A copy has been up for the last 10 min while none of the import's episodes was running."""
    if idle_since is None or now - idle_since < timedelta(seconds=rules.idle_copy_s):
        return False
    recent = [v for t, v in copy_points if t >= now - timedelta(seconds=rules.idle_copy_s)]
    return bool(recent) and all(v > 0 for v in recent)


S7Verdict = Literal["continue", "defer", "pause"]


@dataclass(frozen=True)
class S7:
    due: bool
    projected_usd: float = 0.0
    verdict: S7Verdict = "continue"
    reason: str = ""
    deferred: tuple[str, ...] = ()  # month-fit arms the month no longer fits


def s7_due(settled: int, last_checked: int, rules: Rules) -> bool:
    """After the first 100 settled episodes, then every 250."""
    if settled < rules.s7_first:
        return False
    if last_checked < rules.s7_first:
        return True
    return settled - last_checked >= rules.s7_every


def s7_projection(
    arm: str,
    spent: float,
    settled: int,
    total: int,
    cap: float | None,
    month_projection: float,
    month_fit_projections: Sequence[tuple[str, float]],
    stop_usd: float,
) -> S7:
    """Over its cap, an arm continues while the month (without the month-fit arms not yet started)
    fits under the stop, and pauses otherwise. The month-fit arms, in run order, that no longer fit
    on top are projected to next month."""
    projected = spent / settled * total if settled else spent
    if cap is None or projected <= cap:
        return S7(True, projected)
    over = f"S-7: {arm} projects to ${projected:.2f}, over its ${cap:.2f} cap"
    if month_projection > stop_usd:
        return S7(True, projected, "pause", f"{over}; the month does not fit")
    running = month_projection
    for i, (_, usd) in enumerate(month_fit_projections):
        running += usd
        if running > stop_usd:
            later = tuple(name for name, _ in month_fit_projections[i:])
            return S7(
                True, projected, "defer", f"{over}; the month fits only without {', '.join(later)}", later
            )
    return S7(True, projected, "continue", f"{over}; the month still fits with every month-fit arm")


def s8_admitted(arm: Arm, rules: Rules) -> str | None:
    """GPT's canary cost per episode must be measured and at most $0.060 (A-3, check 4)."""
    if arm.canary_usd_per_episode is None:
        return f"S-8: {arm.name} has no canary cost per episode in the config"
    if arm.canary_usd_per_episode > rules.gpt_admission_usd_per_episode:
        return (
            f"S-8: {arm.name}'s canary cost ${arm.canary_usd_per_episode:.3f}/episode exceeds "
            f"${rules.gpt_admission_usd_per_episode:.3f}; not run in E5"
        )
    return None


def s8_rolling(costs: Sequence[float], canary: float, rules: Rules) -> str | None:
    """Rolling cost per episode over the last 100 results above 1.5 x the canary's."""
    if len(costs) < rules.s8_window:
        return None
    rolling = sum(costs[-rules.s8_window :]) / rules.s8_window
    if rolling > rules.s8_factor * canary:
        return (
            f"S-8: rolling ${rolling:.4f}/episode over {rules.s8_window} is above "
            f"{rules.s8_factor} x canary ${canary:.4f}"
        )
    return None


def s9_budget(month: str, months: dict[str, Month]) -> str | None:
    """Nothing is submitted in a month without an approved stop."""
    rule = months.get(month)
    if rule is None or rule.stop_usd is None:
        return f"S-9: no approved budget for {month}"
    return None


def s9_stop(month: str, months: dict[str, Month], estimate: float) -> str | None:
    """Hard stop: the month's estimate is over its stop."""
    rule = months.get(month)
    if rule is None or rule.stop_usd is None:
        return None
    if estimate > rule.stop_usd:
        return f"S-9: {month} estimate ${estimate:.2f} is over the ${rule.stop_usd:.0f} stop"
    return None


def s9_job_fits(month: str, months: dict[str, Month], estimate: float, job_p90: float) -> str | None:
    """October: a job starts only if the month's estimate plus its P90 stays under the stop."""
    reason = s9_budget(month, months)
    if reason:
        return reason
    rule = months[month]
    assert rule.stop_usd is not None
    if rule.job_p90_check and estimate + job_p90 > rule.stop_usd:
        return f"S-9: {month} estimate ${estimate:.2f} + job P90 ${job_p90:.2f} exceeds ${rule.stop_usd:.0f}"
    return None


MonthFit = Literal["run", "defer", "drop"]


@dataclass(frozen=True)
class MonthFitResult:
    decision: MonthFit
    total_usd: float  # this month's sum up to and including the arm
    arm_usd: float  # the arm's own 1.15 x projection
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "total_usd": round(self.total_usd, 2),
            "arm_usd": round(self.arm_usd, 2),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FitArm:
    """A month-fit arm as section 10.4 sees it: its projection (`fit_projection` of its open episodes),
    whether it has started this month, and whether next month's config lets it run."""

    name: str
    projection_usd: float
    started: bool = False
    next_month: bool = False


def fit_projection(episodes: int, usd_per_episode: float, probe_usd: float, rules: Rules) -> float:
    return rules.month_fit_factor * (episodes * usd_per_episode + probe_usd)


def month_fit_sequence(
    committed: float,
    arms: Sequence[FitArm],
    stop_usd: float | None,
    *,
    next_month_budget: float | None,
    deadline_passed: bool,
) -> dict[str, MonthFitResult]:
    """Section 10.4 for the month-fit arms in run order (GPT-5.6 Sol, then Claude Sonnet 5.5).

    `committed` is the month's metered spend plus its remaining fixed items. An arm runs this month if
    `committed` + the projections of the earlier arms that run this month + its own is at most the
    stop. Otherwise it goes to next month if that month's budget covers its projection on top of the
    earlier deferred arms', and is dropped if no such budget is approved by the deadline. Arms keep
    their order, so once one goes to next month every later one does too. An arm already started
    counts its open projection and keeps running."""
    out: dict[str, MonthFitResult] = {}
    total, deferred = committed, 0.0
    behind: str | None = None
    for arm in arms:
        own = arm.projection_usd
        here = total + own
        if arm.started:
            total = here
            out[arm.name] = MonthFitResult("run", here, own, "started")
            continue
        if behind is None and stop_usd is not None and here <= stop_usd:
            total = here
            out[arm.name] = MonthFitResult("run", here, own, f"${here:.2f} <= ${stop_usd:.0f}")
            continue
        if behind is not None:
            why = f"runs after {behind}, which goes to next month"
        elif stop_usd is None:
            why = "no approved budget this month"
        else:
            why = f"${here:.2f} > ${stop_usd:.0f}"
        covered = next_month_budget is not None and deferred + own <= next_month_budget
        if arm.next_month and covered:
            deferred += own
            behind = arm.name
            out[arm.name] = MonthFitResult(
                "defer", here, own, f"{why}; next month's budget covers ${deferred:.2f}"
            )
        elif deadline_passed:
            out[arm.name] = MonthFitResult("drop", here, own, f"{why}; no next-month budget by the deadline")
        else:
            behind = arm.name
            out[arm.name] = MonthFitResult("defer", here, own, f"{why}; waiting for a next-month budget")
    return out
