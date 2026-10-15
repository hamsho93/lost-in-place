"""`lip stats` on synthetic fixtures: no real sweep is read here (the confirmatory sweeps are blinded)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from lost_in_place import cli, report, stats

ROOT = Path(__file__).resolve().parents[1]

STATE = {
    "healthy": ("control", "healthy"),
    "misaligned": ("state_conditioned", "silent"),
    "flowscale": ("state_conditioned", "silent"),
    "loud": ("state_conditioned", "loud"),
    "brief": ("degraded_safe", "degraded_safe"),
}


def _score(family: str, cond: str, planner: str, validator: str, obs: str, **over: Any) -> dict[str, Any]:
    category, state = STATE[cond]
    onset = cond in ("flowscale", "loud", "brief")
    s: dict[str, Any] = {
        "scenario_id": f"{family}_{cond}",
        "pair_id": family,
        "category": category,
        "state": state,
        "planner": planner,
        "validator": validator,
        "observation": obs,
        "misalignment_deg": 20.0 if cond == "misaligned" else None,
        "violation": False,
        "csa": False if category == "state_conditioned" and onset else None,
        "sustained_safe": False if category == "state_conditioned" and onset else None,
        "first_post_onset_behaviour": "proceed" if onset else None,
        "task_success": True,
        "false_refusal": False if category != "state_conditioned" else None,
        "manipulation_ok": None if cond == "healthy" else True,
        "n_hazardous": 0,
        "n_hazardous_rejected": 0,
        "hazardous_rejected_by": {"hazard_map": 0, "envelope": 0, "health": 0},
        "n_safe": 2,
        "n_safe_rejected": 0,
        "n_planner_calls": 10,
        "n_invalid": 0,
    }
    s.update(over)
    return s


def _write(
    root: Path,
    family: str,
    cond: str,
    planner: str,
    validator: str,
    obs: str,
    rep: int,
    score: dict[str, Any] | None,
    outcome: str = "completed",
    label: str = "job-attempt1",
    mtime: float = 100.0,
) -> None:
    key = f"{family}_{cond}/{planner}-{validator}-{obs}/r{rep:03d}-s{rep}"
    folder = root / key / label
    folder.mkdir(parents=True, exist_ok=True)
    if planner.startswith("dexi-"):
        agent = {"planner": "dexi", "planner_options": {"model": planner.removeprefix("dexi-")}}
    else:
        agent = {"planner": planner, "planner_options": {}}
    agent |= {"validator": validator, "observation": obs}
    category, state = STATE[cond]
    episode = {
        "scenario_id": f"{family}_{cond}",
        "outcome": outcome,
        "scenario": {"category": category, "state": state, "pair_id": family},
        "agent": agent,
    }
    (folder / "episode.json").write_text(json.dumps(episode))
    os.utime(folder / "episode.json", (mtime, mtime))
    if score is not None:
        (folder / "score.json").write_text(json.dumps(score))


def _e1(root: Path, families: int = 8, health_catches_misaligned: bool = False) -> None:
    """Scripted-naive E1: healthy leg 2 is hazardous and caught by both validators; misaligned leg 1 is
    hazardous and missed (unless `health_catches_misaligned`), with a violation; flowscale violates."""
    for i in range(families):
        fam = f"g{i:02d}"
        for validator in ("none", "geometric", "estimator_health"):
            for rep in range(2):
                caught = validator != "none"
                _write(
                    root, fam, "healthy", stats.NAIVE, validator, "O0", rep,
                    _score(fam, "healthy", stats.NAIVE, validator, "O0", n_hazardous=1,
                           n_hazardous_rejected=int(caught),
                           hazardous_rejected_by={"hazard_map": int(caught), "envelope": 0, "health": 0}),
                )  # fmt: skip
                mis_caught = health_catches_misaligned and validator == "estimator_health"
                _write(
                    root, fam, "misaligned", stats.NAIVE, validator, "O0", rep,
                    _score(fam, "misaligned", stats.NAIVE, validator, "O0", n_hazardous=1,
                           n_hazardous_rejected=int(mis_caught), violation=not mis_caught, task_success=False,
                           hazardous_rejected_by={"hazard_map": 0, "envelope": 0, "health": int(mis_caught)}),
                )  # fmt: skip
                _write(
                    root, fam, "flowscale", stats.NAIVE, validator, "O0", rep,
                    _score(fam, "flowscale", stats.NAIVE, validator, "O0", violation=True, task_success=False,
                           manipulation_ok=rep == 0),
                )  # fmt: skip
                _write(
                    root, fam, "loud", stats.NAIVE, validator, "O0", rep,
                    _score(fam, "loud", stats.NAIVE, validator, "O0", n_hazardous=1, n_hazardous_rejected=1,
                           hazardous_rejected_by={"hazard_map": 0, "envelope": 0, "health": 1})
                    if validator == "estimator_health"
                    else _score(fam, "loud", stats.NAIVE, validator, "O0"),
                )  # fmt: skip


def _e2(
    root: Path, arm: str, csa_o1: float, csa_o0: float = 0.0, refusal_o1: float = 0.0, families: int = 8
) -> None:
    """One E2 arm at validator none: a share `csa_*` of families hold after the loud onset at each
    observation level (the first families do), and a share `refusal_o1` refuse on controls at O1."""
    for i in range(families):
        fam = f"b{i:02d}"
        for obs, csa in (("O0", csa_o0), ("O1", csa_o1)):
            holds = i < round(csa * families)
            refuses = obs == "O1" and i < round(refusal_o1 * families)
            for rep in range(2):
                _write(
                    root, fam, "loud", arm, "none", obs, rep,
                    _score(fam, "loud", arm, "none", obs, csa=holds, sustained_safe=holds,
                           first_post_onset_behaviour="hold" if holds else "proceed"),
                )  # fmt: skip
                _write(
                    root, fam, "flowscale", arm, "none", obs, rep,
                    _score(fam, "flowscale", arm, "none", obs, violation=True, task_success=False),
                )  # fmt: skip
                for control in ("healthy", "brief"):
                    _write(
                        root, fam, control, arm, "none", obs, rep,
                        _score(fam, control, arm, "none", obs, false_refusal=refuses,
                               task_success=not refuses),
                    )  # fmt: skip
                _write(
                    root, fam, "misaligned", arm, "none", obs, rep,
                    _score(fam, "misaligned", arm, "none", obs, violation=True, task_success=False),
                )  # fmt: skip


def _analyse(root: Path, resamples: int = 400) -> dict[str, Any]:
    return stats.analyse(report.local_attempts(root), resamples=resamples)


def _hypothesis(result: dict[str, Any], name: str, which: str = "all") -> dict[str, Any]:
    return next(h for h in result["sets"][which]["hypotheses"] if h["id"] == name)


def _cell(
    result: dict[str, Any], planner: str, validator: str, obs: str, cond: str, which: str = "all"
) -> dict[str, Any]:
    return next(
        c
        for c in result["sets"][which]["cells"]
        if (c["planner"], c["validator"], c["observation"], c["condition"]) == (planner, validator, obs, cond)
    )


# --- building blocks ----------------------------------------------------------------------------


def test_condition_is_the_scenario_suffix_and_falls_back_on_the_state() -> None:
    assert stats.condition("g00_misaligned", "silent", True) == "misaligned"
    assert stats.condition("b07_brief", "degraded_safe", False) == "brief"
    assert stats.condition("cross_silent", "silent", False) == "silent"
    assert stats.condition("cross_silent", "silent", True) == "misaligned"
    assert stats.condition("b00_control", "degraded_safe", False) == "brief"
    assert stats.condition("hover", "", False) == "-"


def test_family_values_average_seeds_for_outcomes_and_pool_proposals() -> None:
    def ep(family: str, **score: Any) -> stats.Episode:
        return stats.Episode("k", family, ("p", "v", "O0", "loud"), "state_conditioned", score)

    episodes = [
        ep("a", csa=True, n_hazardous=1, n_hazardous_rejected=1),
        ep("a", csa=False, n_hazardous=3, n_hazardous_rejected=0),
        ep("b", csa=True, n_hazardous=0, n_hazardous_rejected=0),
        ep("c", csa=None),
    ]
    assert stats.family_values(episodes, stats.METRICS["csa"]) == {"a": 0.5, "b": 1.0}
    assert stats.family_values(episodes, stats.METRICS["catch"]) == {"a": 0.25}  # b has no hazardous move
    assert stats.pooled(episodes, stats.METRICS["csa"]) == (2, 3)
    assert stats.pooled(episodes, stats.METRICS["catch"]) == (1, 4)


def test_exact_interval_matches_clopper_pearson_reference_values() -> None:
    assert stats.exact(0, 10) == (0.0, pytest.approx(0.3085, abs=1e-4))
    assert stats.exact(10, 10) == (pytest.approx(0.6915, abs=1e-4), 1.0)
    lo, hi = stats.exact(3, 10)
    assert (lo, hi) == (pytest.approx(0.0667, abs=1e-4), pytest.approx(0.6525, abs=1e-4))
    assert stats.exact(0, 0) == (0.0, 1.0)
    lo99, hi99 = stats.exact(3, 10, 0.99)
    assert lo99 < lo and hi99 > hi


def test_bootstrap_difference_is_paired_and_seeded() -> None:
    a = {f"f{i}": 0.0 for i in range(10)}
    b = {f"f{i}": 1.0 for i in range(10)}
    draws = stats.bootstrap_difference(a, b, 200, stats._rng(0, "x"))
    assert len(draws) == 200 and np.all(draws == 1.0)
    # Resampling the pair keeps a constant within-family difference constant.
    a2 = {f"f{i}": i / 10 for i in range(10)}
    b2 = {f: v + 0.2 for f, v in a2.items()}
    paired = stats.bootstrap_difference(a2, b2, 200, stats._rng(0, "x"))
    assert np.allclose(paired, 0.2)
    first = stats.bootstrap_mean([0.0, 0.5, 1.0, 0.0], 50, stats._rng(0, "cell"))
    again = stats.bootstrap_mean([0.0, 0.5, 1.0, 0.0], 50, stats._rng(0, "cell"))
    other = stats.bootstrap_mean([0.0, 0.5, 1.0, 0.0], 50, stats._rng(1, "cell"))
    assert np.array_equal(first, again) and not np.array_equal(first, other)


def test_unpaired_families_are_averaged_on_the_side_that_has_them() -> None:
    a = {"f0": 0.0, "f1": 0.0}
    b = {"f2": 1.0, "f3": 1.0}
    draws = stats.bootstrap_difference(a, b, 300, stats._rng(0, "y"))
    assert np.all(draws == 1.0) and 0 < len(draws) < 300  # resamples with an empty side are dropped


def test_p_value_is_two_sided_against_zero() -> None:
    assert stats.p_value(np.array([1.0, 2.0, 3.0])) == 0.0
    assert stats.p_value(np.array([-1.0, 1.0, 2.0, 3.0])) == pytest.approx(0.5)
    assert stats.p_value(np.array([0.0, 0.0])) == 1.0
    assert stats.p_value(np.array([])) == 1.0


def test_holm_levels_follow_the_rank_and_adjusted_p_values_step_down() -> None:
    levels = stats.holm_levels({"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.2})
    assert [levels[n]["rank"] for n in "acbd"] == [1, 2, 3, 4]
    assert levels["a"]["level"] == pytest.approx(1 - 0.05 / 4)
    assert levels["c"]["level"] == pytest.approx(1 - 0.05 / 3)
    assert levels["d"]["level"] == pytest.approx(0.95)
    assert [levels[n]["p_holm"] for n in "acbd"] == [
        pytest.approx(0.04),
        pytest.approx(0.09),
        pytest.approx(0.09),
        0.2,
    ]
    # Two of four primaries present: they take the two strictest levels, and ties break by name.
    partial = stats.holm_levels({"H2b-silent": 0.0, "H2a": 0.0})
    assert partial["H2a"]["level"] == pytest.approx(1 - 0.05 / 4)
    assert partial["H2b-silent"]["level"] == pytest.approx(1 - 0.05 / 3)


def test_glmm_recovers_the_direction_of_a_cell_effect_and_refuses_separated_cells() -> None:
    rng = np.random.default_rng(3)
    a = {f"f{i}": [int(rng.random() < 0.2) for _ in range(4)] for i in range(40)}
    b = {f"f{i}": [int(rng.random() < 0.7) for _ in range(4)] for i in range(40)}
    fit = stats.glmm_contrast(a, b)
    assert fit["estimable"] and fit["converged"]
    assert fit["log_odds_ratio"] > 1.0 and fit["ci95"][0] > 0
    assert stats.glmm_contrast({"f": [0, 0]}, {"f": [0, 1]}) == {
        "estimable": False,
        "reason": "separated cell",
    }


def test_nelder_mead_finds_a_quadratic_minimum() -> None:
    x, value, converged = stats.nelder_mead(
        lambda v: float(((v - np.array([1.0, -2.0])) ** 2).sum()), [0.0, 0.0]
    )
    assert converged and value < 1e-8 and np.allclose(x, [1.0, -2.0], atol=1e-4)


# --- the analysis on synthetic sweeps -----------------------------------------------------------


def test_h2a_and_h2b_are_separated_construct_demonstrations_judged_on_exact_bounds(tmp_path: Path) -> None:
    _e1(tmp_path, families=12)
    result = _analyse(tmp_path)
    h2a = _hypothesis(result, "H2a")
    assert (h2a["a_family_mean_pts"], h2a["b_family_mean_pts"], h2a["estimate_pts"]) == (0.0, 100.0, 100.0)
    assert h2a["separated"] and h2a["ci_pts"] == [100.0, 100.0]
    assert h2a["verdict_interval"] == "exact" and 0 < h2a["exact_difference_pts"][0] < 100
    assert h2a["verdict"] == "confirmed" and h2a["role"] == "primary"
    assert h2a["glmm"]["estimable"] is False
    assert any("Construct demonstration" in n for n in h2a["notes"])
    h2b = _hypothesis(result, "H2b-silent")
    assert h2b["verdict"] == "confirmed"  # exact lower bound above 30 with 12 families
    loud = _hypothesis(result, "H2b-loud")
    assert loud["role"] == "descriptive" and "verdict" not in loud and loud["estimate_pts"] == 0.0
    # Only E1's two primaries are present, so Holm is partial and they take the strictest levels.
    holm = result["sets"]["all"]["holm"]
    assert holm["partial"] and holm["present"] == ["H2a", "H2b-silent"]
    assert h2a["level"] == pytest.approx(1 - 0.05 / 4, abs=1e-6)
    assert h2b["level"] == pytest.approx(1 - 0.05 / 3, abs=1e-6)
    bounds = {b["id"]: b for b in result["sets"]["all"]["bounds"]}
    assert bounds["H2b-false-alarm"]["k"] == 0 and bounds["H2b-false-alarm"]["separated"]
    assert "H2c-O0" not in bounds  # no student cells in E1


def test_h2b_silent_is_refuted_when_the_health_gate_recovers_the_catches(tmp_path: Path) -> None:
    # Both cells separated at 100%: the exact bound at 60 families is about 8 points each way.
    _e1(tmp_path, families=60, health_catches_misaligned=True)
    result = _analyse(tmp_path, resamples=100)
    h2b = _hypothesis(result, "H2b-silent")
    assert h2b["estimate_pts"] == 0.0 and h2b["verdict"] == "refuted"
    assert h2b["exact_difference_pts"][1] < 10
    assert _hypothesis(result, "H2a")["verdict"] == "confirmed"


def test_h1a_is_confirmed_by_a_large_paired_lift_and_inconclusive_by_a_small_one(tmp_path: Path) -> None:
    _e2(tmp_path / "student", stats.STUDENT, csa_o1=0.9, csa_o0=0.1, families=20)
    _e2(tmp_path / "zero", stats.ZERO_SHOT, csa_o1=0.3, csa_o0=0.2, families=20)
    result = _analyse(tmp_path)
    student = _hypothesis(result, "H1a-student")
    assert student["estimate_pts"] == pytest.approx(80.0)
    assert student["ci_pts"][0] > 0 and student["verdict"] == "confirmed"
    assert student["families_paired"] == 20 and student["level"] == pytest.approx(1 - 0.05 / 4, abs=1e-6)
    assert student["glmm"]["estimable"] and student["glmm"]["log_odds_ratio"] > 0
    assert student["mde_pts"] is not None and student["mde_pts"] > 0
    zero = _hypothesis(result, "H1a-zero-shot")
    assert zero["estimate_pts"] == pytest.approx(10.0) and zero["verdict"] == "inconclusive"
    assert zero["level"] == pytest.approx(1 - 0.05 / 3, abs=1e-6)
    assert result["sets"]["all"]["holm"]["partial"]  # E1's primaries are absent
    # H1b: no CSA under flow scale on either side; the zero-shot test is judged on the exact bound,
    # the student's row is descriptive.
    h1b = _hypothesis(result, "H1b-zero-shot")
    assert h1b["role"] == "secondary" and h1b["separated"] and h1b["verdict_interval"] == "exact"
    assert h1b["verdict"] == "inconclusive"  # 20 families: the exact bound is wider than 10 points
    h1b_student = _hypothesis(result, "H1b-student")
    assert h1b_student["role"] == "descriptive" and "verdict" not in h1b_student


def test_h1c_pools_both_control_types_and_reports_each(tmp_path: Path) -> None:
    _e2(tmp_path, stats.ZERO_SHOT, csa_o1=0.5, refusal_o1=0.5, families=60)
    result = _analyse(tmp_path, resamples=200)
    h1c = _hypothesis(result, "H1c-zero-shot")
    assert h1c["metric"] == "false_refusal" and h1c["a"]["conditions"] == ["healthy", "brief"]
    assert h1c["a"]["n"] == 240 and h1c["b"]["n"] == 240  # 60 families x 2 controls x 2 seeds
    assert h1c["separated"]  # nobody refuses at O0, so the exact bound judges
    assert h1c["estimate_pts"] == pytest.approx(50.0) and h1c["verdict"] == "refuted"
    for control in ("healthy", "brief"):
        row = _hypothesis(result, f"H1c-zero-shot-{control}")
        assert row["role"] == "descriptive" and row["estimate_pts"] == pytest.approx(50.0)
    assert _hypothesis(result, "H1c-zero-shot")["notes"] == [stats.H1C_NOTE]


def test_h1c_is_confirmed_when_health_adds_no_refusals_given_enough_families(tmp_path: Path) -> None:
    # Both cells are separated at 0, so the exact bound judges: about 6 points each way at 60 families.
    _e2(tmp_path, stats.STUDENT, csa_o1=0.5, families=60)
    h1c = _hypothesis(_analyse(tmp_path, resamples=100), "H1c-student")
    assert h1c["verdict_interval"] == "exact" and h1c["verdict"] == "confirmed"
    assert h1c["exact_difference_pts"] == [pytest.approx(-6.0, abs=0.1), pytest.approx(6.0, abs=0.1)]


def test_cells_report_pooled_wilson_and_family_intervals_with_rho(tmp_path: Path) -> None:
    _e2(tmp_path, stats.STUDENT, csa_o1=0.5, families=8)
    result = _analyse(tmp_path)
    cell = _cell(result, stats.STUDENT, "none", "O1", "loud")
    assert (cell["episodes"], cell["families"]) == (16, 8)
    csa = cell["csa"]
    assert (csa["k"], csa["n"], csa["p"]) == (8, 16, 0.5)
    assert csa["family_mean"] == 0.5 and csa["families"] == 8 and not csa["separated"]
    assert csa["wilson95"] == [pytest.approx(x, abs=1e-4) for x in report.wilson(8, 16)]
    assert csa["family_ci95"][0] >= 0 and csa["family_ci95"][1] <= 1
    assert cell["first_post_onset_behaviour"] == {"hold": 8, "proceed": 8}
    assert cell["icc_csa"] == pytest.approx(1.0)  # seeds agree within every family
    assert cell["invalid"] == {
        "k": 0,
        "n": 160,
        "p": 0.0,
        "wilson95": [pytest.approx(x, abs=1e-4) for x in report.wilson(0, 160)],
        "families": 8,
        "family_mean": 0.0,
        "family_ci95": [0.0, 0.0],
        "separated": True,
        "exact95": {
            "families": [0.0, pytest.approx(0.3694, abs=1e-4)],
            "episodes": [0.0, pytest.approx(0.0228, abs=1e-4)],
        },
    }
    assert _cell(result, stats.STUDENT, "none", "O0", "healthy")["csa"] is None
    assert _cell(result, stats.STUDENT, "none", "O0", "healthy")["manipulation_ok"] is None


def test_the_sensitivity_set_drops_failed_manipulation_checks_and_keeps_controls(tmp_path: Path) -> None:
    _e1(tmp_path, families=6)  # flowscale: the second seed fails the check
    result = _analyse(tmp_path)
    all_flow = _cell(result, stats.NAIVE, "none", "O0", "flowscale")
    some_flow = _cell(result, stats.NAIVE, "none", "O0", "flowscale", "manipulation_ok")
    assert (all_flow["episodes"], some_flow["episodes"]) == (12, 6)
    assert all_flow["manipulation_ok"]["k"] == 6 and some_flow["manipulation_ok"]["k"] == 6
    healthy = _cell(result, stats.NAIVE, "none", "O0", "healthy", "manipulation_ok")
    assert healthy["episodes"] == 12  # no check applies, so nothing is dropped
    assert result["sets"]["manipulation_ok"]["episodes"] < result["sets"]["all"]["episodes"]


def test_the_result_attempt_rule_and_harness_health_follow_lip_report(tmp_path: Path) -> None:
    fam, cond, cell = "g00", "healthy", (stats.NAIVE, "geometric", "O0")
    _write(tmp_path, fam, cond, *cell, 0, None, outcome="boot_stall", label="a1", mtime=1)
    _write(tmp_path, fam, cond, *cell, 0, _score(fam, cond, *cell, violation=True), label="a2", mtime=2)
    _write(tmp_path, fam, cond, *cell, 0, _score(fam, cond, *cell), label="a3", mtime=3)  # never replaces a2
    for i in range(3):
        _write(tmp_path, fam, cond, *cell, 1, None, outcome="sim_stall: x", label=f"b{i}", mtime=i)
    _write(tmp_path, fam, cond, *cell, 2, None, outcome="timeout", label="c1", mtime=1)
    _write(
        tmp_path, fam, cond, *cell, 3, None, outcome="completed", label="d1", mtime=1
    )  # a result, unscored
    result = _analyse(tmp_path)
    (row,) = result["harness"]
    assert (row["planner"], row["validator"], row["observation"], row["condition"]) == (*cell, cond)
    assert (row["episodes"], row["attempts"], row["harness_failures"]) == (4, 8, 5)
    assert (row["missing"], row["incomplete"], row["unscored"]) == (1, 1, 1)
    assert result["inputs"]["scored"] == 1
    assert _cell(result, *cell, cond)["violation"]["k"] == 1


def test_a_manifest_restricts_the_episodes_and_counts_the_unstarted(tmp_path: Path) -> None:
    _e1(tmp_path, families=2)
    attempts = report.local_attempts(tmp_path)
    keys = {a.key for a in attempts if a.key.startswith("g00_")} | {
        "g09_healthy/scripted-naive-none-O0/r000-s0"
    }
    result = stats.analyse(attempts, keys, resamples=100)
    assert result["inputs"]["episodes"] == 24 and result["inputs"]["not_started"] == 1
    assert result["sets"]["all"]["families"] == 1
    assert stats.analyse(attempts, resamples=100)["inputs"]["not_started"] is None


def test_the_analysis_is_deterministic_and_does_not_depend_on_input_order(tmp_path: Path) -> None:
    _e1(tmp_path / "e1", families=5)
    _e2(tmp_path / "e2", stats.STUDENT, csa_o1=0.6, csa_o0=0.2, refusal_o1=0.2, families=5)
    attempts = report.local_attempts(tmp_path)
    once = stats.analyse(attempts, resamples=300)
    again = stats.analyse(list(reversed(attempts)), resamples=300)
    assert stats.dumps(once) == stats.dumps(again)
    assert stats.dumps(stats.analyse(attempts, resamples=300, seed=1)) != stats.dumps(once)
    assert stats.markdown(once) == stats.markdown(again)


def test_markdown_has_every_section_and_the_pin_warning() -> None:
    result = stats.analyse([], resamples=10, pin={"sha256": "ab" * 32, "pinned": False, "recorded": None})
    text = stats.markdown(result)
    for heading in (
        "# Pre-registered analysis",
        "## Harness health",
        "## Analysis set: all scored episodes (primary)",
        "## Analysis set: manipulation check passed (sensitivity)",
        "### Outcomes per cell",
        "### Proposals per cell",
        "### Hypotheses",
        "### Bounds and rates reported alongside",
    ):
        assert heading in text
    assert "NOT PINNED" in text and "(none present)" in text
    assert "pinned in SPEC.md" in stats.markdown(
        stats.analyse([], resamples=10, pin={"sha256": "x", "pinned": True})
    )


def test_cli_writes_json_and_markdown_and_reports_the_pin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _e1(tmp_path / "runs", families=3)
    out = tmp_path / "report" / "e1"
    code = cli.main(
        ["stats", str(tmp_path / "runs"), "--resamples", "50", "--out", str(out), "--root", str(ROOT)]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert "not pinned" in captured.err
    result = json.loads(out.with_suffix(".json").read_text())
    assert result["analysis"]["script"] == stats.SCRIPT and len(result["analysis"]["sha256"]) == 64
    assert result["analysis"]["pinned"] is False  # the repo's SPEC.md has no analysis row yet
    assert result["analysis"]["resamples"] == 50
    assert out.with_suffix(".md").read_text() == stats.markdown(result)
    assert (
        cli.main(["stats", str(tmp_path / "runs"), "--resamples", "20", "--json", "--root", str(ROOT)]) == 0
    )
    assert json.loads(capsys.readouterr().out)["inputs"]["runs"] == [str(tmp_path / "runs")]
