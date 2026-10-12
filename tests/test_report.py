import json
import os
from pathlib import Path
from typing import Any

import pytest

from lost_in_place import report


def test_wilson_matches_reference_values() -> None:
    lo, hi = report.wilson(5, 10)
    assert (lo, hi) == (pytest.approx(0.2366, abs=1e-4), pytest.approx(0.7634, abs=1e-4))
    lo, hi = report.wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-4)
    assert report.wilson(0, 0) == (0.0, 1.0)


def test_icc_is_one_when_scenarios_decide_the_outcome_and_low_when_they_do_not() -> None:
    assert report.icc({"a": [1, 1, 1], "b": [0, 0, 0]}) == pytest.approx(1.0)
    assert report.icc({"a": [1, 0, 1, 0], "b": [0, 1, 0, 1]}) < 0
    assert report.icc({"a": [1, 1]}) is None
    assert report.icc({"a": [0, 0], "b": [0, 0]}) is None


def test_llm_attempts_are_reported_under_their_model() -> None:
    agent = {"planner": "llm", "planner_options": {"model": "m1"}, "validator": "none", "observation": "O0"}
    a = report._attempt("k", "l", 0.0, {"agent": agent, "scenario": {"state": "loud"}}, None)
    assert a.cell[report.CELL_FIELDS.index("planner")] == "m1"


def test_counted_is_the_earliest_attempt_that_is_not_a_harness_failure() -> None:
    def a(label: str, modified: float, outcome: str) -> report.Attempt:
        return report.Attempt("k", label, modified, outcome, "s", ("c",), None)

    tries = [a("late", 3, "completed"), a("crash", 1, "error: reset"), a("first", 2, "completed")]
    assert report.counted(tries) == tries[2]
    assert report.counted([a("x", 1, "timeout"), a("y", 2, "sim_stall")]) is None


def _attempt(
    root: Path,
    key: str,
    label: str,
    mtime: float,
    outcome: str,
    score: dict[str, Any] | None = None,
    state: str = "silent",
    validator: str = "geometric",
) -> None:
    folder = root / key / label
    folder.mkdir(parents=True)
    episode = {
        "scenario_id": key.split("/")[0],
        "outcome": outcome,
        "scenario": {"category": "state_conditioned", "state": state},
        "agent": {"planner": "scripted-naive", "validator": validator, "observation": "O0"},
    }
    (folder / "episode.json").write_text(json.dumps(episode))
    os.utime(folder / "episode.json", (mtime, mtime))
    if score is not None:
        (folder / "score.json").write_text(json.dumps(score))


def _score(violation: bool, csa: bool, hazardous: int = 0, caught: int = 0) -> dict[str, Any]:
    return {
        "violation": violation,
        "csa": csa,
        "task_success": False,
        "false_refusal": None,
        "manipulation_ok": True,
        "n_hazardous": hazardous,
        "n_hazardous_rejected": caught,
        "n_safe": 2,
        "n_safe_rejected": 0,
    }


def test_summary_counts_the_first_valid_attempt_and_every_harness_failure(tmp_path: Path) -> None:
    cell = "scripted-naive-geometric-O0"
    # A harness failure, then a result: the result counts and the failure is reported.
    _attempt(tmp_path, f"cross_silent/{cell}/r000-s1", "job1-attempt1", 100, "boot_stall")
    _attempt(
        tmp_path, f"cross_silent/{cell}/r000-s1", "job1-attempt2", 200, "completed", _score(True, False, 2, 1)
    )
    # A later rerun of a settled episode never replaces its first result.
    _attempt(tmp_path, f"cross_silent/{cell}/r000-s1", "job2-attempt1", 300, "completed", _score(False, True))
    _attempt(
        tmp_path, f"cross_silent/{cell}/r001-s2", "job1-attempt1", 100, "completed", _score(True, False, 1, 0)
    )
    _attempt(
        tmp_path, f"corner_silent/{cell}/r000-s3", "job1-attempt1", 100, "completed", _score(False, True)
    )
    # Three harness failures: missing, not retried again, not counted as a result.
    for i in range(3):
        _attempt(tmp_path, f"corner_silent/{cell}/r001-s4", f"job1-attempt{i + 1}", 100 + i, "sim_stall: x")

    summary = report.summarize(report.local_attempts(tmp_path))
    assert summary["episodes"] == 4
    (row,) = summary["cells"]
    assert (row["state"], row["validator"]) == ("silent", "geometric")
    assert (row["attempts"], row["harness_failures"], row["missing"], row["results"]) == (8, 4, 1, 3)
    assert row["violation"]["k"] == 2 and row["violation"]["n"] == 3
    assert row["csa"]["k"] == 1
    assert (row["catch"]["k"], row["catch"]["n"]) == (1, 3)
    assert (row["false_alarm"]["k"], row["false_alarm"]["n"]) == (0, 6)
    assert row["icc_violation"] == pytest.approx(1.0)  # cross always violates, corner never does
    assert "| silent | state_conditioned | scripted-naive | geometric |" in report.markdown(summary)


def test_cells_split_by_state_and_validator(tmp_path: Path) -> None:
    for state in ("healthy", "loud"):
        for validator in ("none", "estimator_health"):
            key = f"cross_{state}/scripted-naive-{validator}-O0/r000-s1"
            _attempt(tmp_path, key, "a1", 100, "completed", _score(False, False), state, validator)
    cells = report.summarize(report.local_attempts(tmp_path))["cells"]
    assert [(c["state"], c["validator"]) for c in cells] == [
        ("healthy", "estimator_health"),
        ("healthy", "none"),
        ("loud", "estimator_health"),
        ("loud", "none"),
    ]


def test_a_manifest_restricts_the_report_and_counts_unstarted_episodes(tmp_path: Path) -> None:
    key = "cross_loud/scripted-naive-none-O0/r000-s1"
    _attempt(tmp_path, key, "a1", 100, "completed", _score(False, False), "loud", "none")
    _attempt(tmp_path, "textured_hover_s100", "a1", 100, "completed")
    attempts = report.local_attempts(tmp_path)
    assert report.summarize(attempts)["episodes"] == 2
    summary = report.summarize(attempts, {key, "cross_loud/scripted-naive-none-O0/r001-s2"})
    assert (summary["episodes"], summary["not_started"]) == (1, 1)
    assert report.markdown(summary).startswith("1 episodes with attempts, 1 not started")
