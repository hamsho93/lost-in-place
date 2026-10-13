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

import pytest

from lost_in_place import families
from lost_in_place.scenario import load_scenario

ROOT = Path(__file__).parents[1] / "scenarios"
PATHS = sorted([*(ROOT / "e1").glob("*.yaml"), *(ROOT / "e2").glob("*.yaml")])
SCENARIOS = {p.stem: load_scenario(p) for p in PATHS}
FAMILIES = sorted({s.pair_id for s in SCENARIOS.values() if s.pair_id})


def _family(name: str) -> dict:
    return {suffix: SCENARIOS[f"{name}_{suffix}"] for suffix in families.SUFFIXES}


def test_every_family_has_all_four_states() -> None:
    assert len(FAMILIES) >= 3
    by_family: dict[str, set[str]] = defaultdict(set)
    for name, s in SCENARIOS.items():
        family, suffix = name.rsplit("_", 1)
        assert s.pair_id == family
        by_family[family].add(suffix)
    assert all(states == set(families.SUFFIXES) for states in by_family.values())


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_matched_scenarios_differ_only_in_the_fault(name: str) -> None:
    s = SCENARIOS[name]
    control = SCENARIOS[f"{s.pair_id}_healthy"]
    ignore = {"id", "state", "category", "steps", "expected_safe_behaviour", "misalignment_deg"}
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
    assert {s.category for k, s in scenarios.items() if k != "healthy"} == {"state_conditioned"}
    # The hazardous checker rejects it: its second leg is safe.
    as_hazard = scenarios["healthy"].model_copy(update={"category": "instruction_hazard"})
    hazardous = {**scenarios, "healthy": as_hazard}
    assert "healthy: geometric can't see the second leg's hazard" in families.problems(hazardous)


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
