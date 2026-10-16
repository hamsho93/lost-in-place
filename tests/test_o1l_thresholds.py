import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "o1l_thresholds.py"
COMMITTED = Path(__file__).resolve().parents[1] / "docs" / "prereg" / "o1l-thresholds.json"


@pytest.fixture(scope="module")
def o1l() -> ModuleType:
    spec = importlib.util.spec_from_file_location("o1l_thresholds", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _decision(flow: float, eph: float, continued: bool = False) -> dict[str, Any]:
    health = {"xy_valid": True, "eph_m": eph, "flow_quality": flow}
    return {"continued": continued, "health": health, "action": {"name": "move"}}


def _write(
    root: Path,
    run: str,
    key: str,
    label: str,
    mtime: float,
    decisions: list[dict[str, Any]],
    outcome: str = "completed",
    category: str = "control",
    state: str = "healthy",
    observation: str = "O1",
    model: str = "m",
) -> None:
    folder = root / run / key / label
    folder.mkdir(parents=True)
    episode = {
        "outcome": outcome,
        "scenario": {"category": category, "state": state},
        "agent": {"planner": "dexi", "planner_options": {"model": model}, "observation": observation},
        "decisions": decisions,
    }
    (folder / "episode.json").write_text(json.dumps(episode))
    os.utime(folder / "episode.json", (mtime, mtime))


def _root(tmp_path: Path) -> Path:
    for i, run in enumerate(("e2-student", "e2-zero-shot", "e4-claude")):
        flows = [170.0 + i, 165.0, 180.0]
        _write(tmp_path, run, "h/O0/r0", "a1", 1, [_decision(f, 0.3) for f in flows], observation="O0")
        _write(tmp_path, run, "h/O1/r0", "a1", 1, [_decision(175.0, 0.61 + 0.01 * i)], model=f"m{i}")
    _write(tmp_path, "e2-student", "h/O1/r0", "a0", 0, [_decision(5.0, 9.0)], outcome="timeout")
    _write(tmp_path, "e2-student", "loud/O1/r0", "a1", 1, [_decision(0.0, 9.0)], "completed", "x", "loud")
    _write(tmp_path, "e4-claude", "h/O0/r1", "a1", 1, [_decision(0.0, 9.0, continued=True)], "completed")
    return tmp_path


def test_reads_only_counted_non_continued_healthy_decisions(o1l: ModuleType, tmp_path: Path) -> None:
    result = o1l.thresholds(str(_root(tmp_path)))
    assert result["pooled"]["decisions"] == 12
    assert result["pooled"]["episodes"] == 7
    assert result["pooled"]["flow_quality_p1"] == pytest.approx(165.0)
    assert result["FLOW"] == 160
    assert result["EPH"] == 0.7
    assert set(result["per_arm"]) == {"dexi-m", "dexi-m0", "dexi-m1", "dexi-m2"}
    assert result["per_arm"]["dexi-m"]["runs"] == ["e2-student", "e2-zero-shot", "e4-claude"]
    assert result["per_arm"]["dexi-m"]["by_observation"]["O0"]["decisions"] == 9


def test_flow_rounds_down_to_ten_and_eph_up_to_a_decimetre(o1l: ModuleType, tmp_path: Path) -> None:
    for run in ("e2-student", "e2-zero-shot", "e4-claude"):
        _write(tmp_path, run, "h/O1/r0", "a1", 1, [_decision(150.0, 0.6)] * 3)
    result = o1l.thresholds(str(tmp_path))
    assert (result["FLOW"], result["EPH"]) == (150, 0.6)


def test_output_is_identical_on_rerun_and_names_no_root(o1l: ModuleType, tmp_path: Path) -> None:
    root = _root(tmp_path / "runs")
    out = tmp_path / "out.json"
    assert o1l.main([str(root), "--out", str(out)]) == 0
    assert o1l.main([str(root), "--out", str(out), "--check"]) == 0
    assert str(root) not in out.read_text()


def test_committed_thresholds_match_the_documented_rule() -> None:
    committed = json.loads(COMMITTED.read_text())
    assert committed["runs"] == ["e2-student", "e2-zero-shot", "e4-claude"]
    assert committed["FLOW"] == committed["pooled"]["flow_quality_p1"] // 10 * 10
    assert committed["EPH"] * 10 == pytest.approx(-(-committed["pooled"]["eph_m_p99"] * 10 // 1))
