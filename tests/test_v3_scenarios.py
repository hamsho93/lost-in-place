"""The audit-fixes smoke set, `scenarios/smoke-audit-fixes/` (first committed as `scenarios/v3/`):
E2's benign families, with more of them and with the loud fault anchored to the first move's target
(`families.LOUD_ANCHOR_M`) instead of timed 2.5 s into it. The frozen E1 and E2 sets are unchanged
(their hashes are in SPEC's freeze record)."""

from pathlib import Path

import pytest

from lost_in_place import families
from lost_in_place.scenario import Inject, SetParam, load_scenario

ROOT = Path(__file__).parents[1] / "scenarios"
V3, E2 = ROOT / "smoke-audit-fixes", ROOT / "e2"
SCENARIOS = {p.stem: load_scenario(p) for p in sorted(V3.glob("*.yaml"))}
NAMES = [f"b{i:02d}" for i in range(100)]


def _family(name: str) -> dict:
    return {suffix: SCENARIOS[f"{name}_{suffix}"] for suffix in families.BENIGN_SUFFIXES}


def test_v3_has_100_benign_families_in_five_states() -> None:
    assert sorted(SCENARIOS) == sorted(f"{n}_{s}" for n in NAMES for s in families.BENIGN_SUFFIXES)
    assert {s.category for k, s in SCENARIOS.items() if k.endswith("_healthy")} == {"control"}


@pytest.mark.parametrize("name", NAMES)
def test_v3_family_geometry(name: str) -> None:
    assert families.problems(_family(name)) == []


@pytest.mark.parametrize("name", NAMES)
def test_v3_files_are_what_the_generator_writes(name: str, tmp_path: Path) -> None:
    for path in families.generate(name, 0, benign=True, loud_anchored=True).write(tmp_path):
        assert path.read_bytes() == (V3 / path.name).read_bytes(), path.name


def test_every_v3_loud_fault_starts_2_m_from_the_first_moves_target() -> None:
    for name in NAMES:
        loud = SCENARIOS[f"{name}_loud"]
        assert families.loud_timing(loud) == families.ANCHORED_LOUD == ("first_move_target", 2.0, (0.0,))
        assert [s for s in loud.steps if isinstance(s, SetParam)] == [
            SetParam(at_s=0.0, name="EKF2_NOAID_TOUT", value=10_000_000, type="int")
        ]
        assert [(s.type, s.at_s) for s in loud.steps if isinstance(s, Inject)] == [("off", 0.0)]


def test_the_brief_control_keeps_its_anchor() -> None:
    for name in NAMES:
        brief = SCENARIOS[f"{name}_brief"].mission
        assert brief is not None
        assert (brief.fault_anchor, brief.fault_anchor_m) == ("first_move_target", families.BRIEF_ANCHOR_M)


def test_the_e2_families_carry_over_and_only_their_loud_files_change() -> None:
    """b00-b59 pair with E2 and E4 episode for episode (same names, so the same seeds)."""
    e2 = sorted(p.name for p in E2.glob("*.yaml"))
    assert len(e2) == 300
    for name in e2:
        same = (E2 / name).read_bytes() == (V3 / name).read_bytes()
        assert same is not name.endswith("_loud.yaml"), name
    for name in NAMES[:60]:
        old, new = load_scenario(E2 / f"{name}_loud.yaml"), SCENARIOS[f"{name}_loud"]
        assert families.loud_timing(old) == families.TIMED_LOUD
        assert old.model_dump(exclude={"mission", "steps"}) == new.model_dump(exclude={"mission", "steps"})
        assert old.mission is not None and new.mission is not None
        ignore = {"fault_anchor", "fault_anchor_m"}
        assert old.mission.model_dump(exclude=ignore) == new.mission.model_dump(exclude=ignore)


def test_problems_reports_a_loud_fault_on_neither_timing() -> None:
    family = _family("b00")
    loud = family["loud"]
    assert loud.mission is not None
    late = loud.mission.model_copy(update={"fault_anchor_m": 1.8})
    family["loud"] = loud.model_copy(update={"mission": late})
    expected = "loud: flow off neither 2.5 s into the first move nor 2.0 m short of its target"
    assert expected in families.problems(family)
    timed = loud.mission.model_copy(update={"fault_anchor": "first_move", "fault_anchor_m": None})
    family["loud"] = loud.model_copy(update={"mission": timed})  # anchored offsets, timed anchor
    assert expected in families.problems(family)


def test_the_loud_anchor_changes_only_the_loud_file() -> None:
    timed = families.generate("b07", 0, benign=True).documents()
    anchored = families.generate("b07", 0, benign=True, loud_anchored=True).documents()
    timed_loud, anchored_loud = timed.pop("loud"), anchored.pop("loud")
    assert timed == anchored
    assert anchored_loud["mission"]["fault_anchor_m"] == families.LOUD_ANCHOR_M
    assert [s["at_s"] for s in timed_loud["steps"]] == [0.0, families.LOUD_AT_S, families.END_S]
    assert [s["at_s"] for s in anchored_loud["steps"]] == [0.0, 0.0, families.END_S]
    for doc in (timed_loud, anchored_loud):
        del doc["mission"], doc["steps"]
    assert timed_loud == anchored_loud


def test_families_command_writes_anchored_loud_files(tmp_path: Path) -> None:
    from lost_in_place import cli

    args = ["families", "--count", "1", "--prefix", "b", "--benign", "--loud-anchor", "--out", str(tmp_path)]
    assert cli.main(args) == 0
    assert (tmp_path / "b00_loud.yaml").read_bytes() == (V3 / "b00_loud.yaml").read_bytes()
