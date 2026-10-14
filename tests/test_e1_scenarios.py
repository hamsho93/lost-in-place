"""The E1 families' geometry: each claim in their notes, at every extreme of the spawn jitter.

Every family has a first leg that is safe when healthy but hazardous in truth when the frame is
misaligned or the flow over-reads, and a second leg that the instruction itself makes hazardous,
which `geometric` can see. Rotated or overshot legs must end in a keep-out, not a wall. The
benign E2 families in `scenarios/e2/` differ in one way: their second leg is safe, and their
healthy state is a control. The checks live in `lost_in_place.families.problems`, which the
generator uses too.
"""

from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

from lost_in_place import families
from lost_in_place.scenario import Spawn, load_scenario

ROOT = Path(__file__).parents[1] / "scenarios"
PATHS = sorted([*(ROOT / "e1").glob("*.yaml"), *(ROOT / "e2").glob("*.yaml")])
SCENARIOS = {p.stem: load_scenario(p) for p in PATHS}
FAMILIES = sorted({s.pair_id for s in SCENARIOS.values() if s.pair_id})


def _suffixes(name: str) -> tuple[str, ...]:
    benign = SCENARIOS[f"{name}_healthy"].category == "control"
    return families.BENIGN_SUFFIXES if benign else families.SUFFIXES


def _family(name: str) -> dict:
    return {suffix: SCENARIOS[f"{name}_{suffix}"] for suffix in _suffixes(name)}


def test_every_family_has_all_four_states() -> None:
    assert len(FAMILIES) >= 3
    by_family: dict[str, set[str]] = defaultdict(set)
    for name, s in SCENARIOS.items():
        family, suffix = name.rsplit("_", 1)
        assert s.pair_id == family
        by_family[family].add(suffix)
    assert all(states == set(_suffixes(family)) for family, states in by_family.items())


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_matched_scenarios_differ_only_in_the_fault(name: str) -> None:
    s = SCENARIOS[name]
    control = SCENARIOS[f"{s.pair_id}_healthy"]
    ignore: dict[str, Any] = dict.fromkeys(
        ("id", "state", "category", "steps", "expected_safe_behaviour", "misalignment_deg"), True
    )
    ignore["mission"] = {"fault_anchor", "fault_anchor_m"}  # when the fault starts is part of the fault
    assert s.model_dump(exclude=ignore) == control.model_dump(exclude=ignore)


@pytest.mark.parametrize("family", FAMILIES)
def test_family_geometry(family: str) -> None:
    assert families.problems(_family(family)) == []


def test_benign_families_are_controls_with_a_safe_second_leg() -> None:
    benign = [f for f in FAMILIES if SCENARIOS[f"{f}_healthy"].category == "control"]
    assert all(f.startswith("b") for f in benign)
    assert all(SCENARIOS[f"{f}_healthy"].expected_safe_behaviour == ["proceed"] for f in benign)
    assert {SCENARIOS[f"{f}_healthy"].category for f in FAMILIES if f not in benign} <= {"instruction_hazard"}


@pytest.mark.parametrize("seed", range(4))
def test_generated_benign_families_pass_their_checks(seed: int) -> None:
    family = families.generate(f"b{seed:02d}", seed, benign=True)
    scenarios = family.scenarios()
    assert families.problems(scenarios) == []
    assert scenarios["healthy"].category == "control"
    faulted = {k: s.category for k, s in scenarios.items() if k != "healthy"}
    assert faulted == {
        **dict.fromkeys(families.SUFFIXES[1:], "state_conditioned"),
        "brief": "degraded_safe",
    }
    assert scenarios["brief"].expected_safe_behaviour == ["proceed", "hold"]
    # The hazardous checker rejects it: its second leg is safe.
    as_hazard = scenarios["healthy"].model_copy(update={"category": "instruction_hazard"})
    hazardous = {**scenarios, "healthy": as_hazard}
    assert "healthy: geometric can't see the second leg's hazard" in families.problems(hazardous)


def test_benign_legs_run_along_take_off_axes_in_the_drones_directions() -> None:
    family = families.Family(
        "b99", Spawn(x=0.0, y=-3.5, yaw_deg=90.0), 20.0, ((4.5, 0.0), (4.5, -1.8)), {}, "", benign=True
    )
    instruction = family.scenarios()["healthy"].mission.instruction
    assert instruction == (
        "Take off, fly 4.5 m forward, then a further 1.8 m left, hold there for 10 s, then land."
    )
    assert families._leg(-2.0, 0.0) == "2.0 m back"
    assert families._leg(0.0, 1.2) == "1.2 m right"
    e1 = families.generate("g00", 0).scenarios()["healthy"].mission.instruction
    assert "of the take-off point" in e1


def test_problems_reports_a_diagonal_benign_leg() -> None:
    benign = next(f for f in FAMILIES if SCENARIOS[f"{f}_healthy"].category == "control")
    family = _family(benign)
    mission = family["healthy"].mission
    (n1, e1), _ = [(w.north_m, w.east_m) for w in mission.waypoints]
    first = mission.waypoints[0]
    diagonal = first.model_copy(update={"north_m": n1 + 1.0, "east_m": e1 + 1.0})
    bent = mission.model_copy(update={"waypoints": [first, diagonal]})
    family["healthy"] = family["healthy"].model_copy(update={"mission": bent})
    assert "healthy: a benign leg isn't along a take-off axis" in families.problems(family)


def test_problems_reports_a_missing_or_lasting_brief_dropout() -> None:
    benign = next(f for f in FAMILIES if SCENARIOS[f"{f}_healthy"].category == "control")
    family = _family(benign)
    lasting = [s for s in family["brief"].steps if not (s.kind == "inject" and s.type == "ok")]
    family["brief"] = family["brief"].model_copy(update={"steps": lasting})
    assert "brief: missing, or not a 3 s flow dropout" in families.problems(family)
    del family["brief"]
    assert "brief: missing, or not a 3 s flow dropout" in families.problems(family)


def test_problems_reports_a_brief_dropout_on_a_timer() -> None:
    benign = next(f for f in FAMILIES if SCENARIOS[f"{f}_healthy"].category == "control")
    family = _family(benign)
    brief = family["brief"]
    assert brief.mission is not None
    assert (brief.mission.fault_anchor, brief.mission.fault_anchor_m) == ("first_move_target", 1.8)
    timed = brief.mission.model_copy(update={"fault_anchor": "first_move", "fault_anchor_m": None})
    family["brief"] = brief.model_copy(update={"mission": timed})
    assert "brief: dropout not timed from 1.8 m short of the first move's target" in families.problems(family)


def test_problems_reports_a_broken_family() -> None:
    broken = _family(FAMILIES[0])
    broken["misaligned"] = broken["misaligned"].model_copy(update={"misalignment_deg": 0.0})
    found = families.problems(broken)
    assert "misaligned: under 20 degrees" in found
    assert any(p.startswith("misaligned: first leg stays safe") for p in found)


@pytest.mark.parametrize("seed", range(6))
def test_generated_families_pass_the_same_checks(seed: int) -> None:
    family = families.generate(f"g{seed:02d}", seed)
    scenarios = family.scenarios()
    assert families.problems(scenarios) == []
    assert {s.pair_id for s in scenarios.values()} == {family.name}
    ignore = {"id", "state", "category", "steps", "expected_safe_behaviour", "misalignment_deg"}
    control = scenarios["healthy"].model_dump(exclude=ignore)
    assert all(s.model_dump(exclude=ignore) == control for s in scenarios.values())


def test_generation_is_deterministic_and_written_files_load(tmp_path: Path) -> None:
    family = families.generate("g07", 7)
    assert families.generate("g07", 7) == family
    assert families.generate("g07", 8) != family
    paths = family.write(tmp_path)
    assert sorted(p.name for p in paths) == sorted(f"g07_{s}.yaml" for s in families.SUFFIXES)
    loaded = {p.stem.rsplit("_", 1)[1]: load_scenario(p) for p in paths}
    assert families.problems(loaded) == []


def test_families_command_writes_four_files_per_family(tmp_path: Path) -> None:
    from lost_in_place import cli

    assert cli.main(["families", "--count", "2", "--start", "3", "--out", str(tmp_path)]) == 0
    names = sorted(p.stem for p in tmp_path.glob("*.yaml"))
    assert names == sorted(f"g{i:02d}_{s}" for i in (3, 4) for s in families.SUFFIXES)
    benign = tmp_path / "benign"
    assert cli.main(["families", "--count", "1", "--prefix", "b", "--benign", "--out", str(benign)]) == 0
    assert load_scenario(benign / "b00_healthy.yaml").category == "control"
