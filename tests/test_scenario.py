from pathlib import Path

import pytest
from pydantic import ValidationError

from lost_in_place.scenario import End, Inject, Scenario, Spawn, load_scenario

FIXTURES = sorted((Path(__file__).parents[1] / "scenarios" / "fixtures").glob("*.yaml"))


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_fixture_scenarios_load(path: Path) -> None:
    scenario = load_scenario(path)
    assert scenario.id == path.stem
    assert isinstance(scenario.steps[-1], End)


def test_spawn_from_seed_is_deterministic_and_bounded() -> None:
    a, b = Spawn.from_seed(7), Spawn.from_seed(7)
    assert a == b
    assert Spawn.from_seed(8) != a
    assert -4 <= a.x <= 4 and -4 <= a.y <= 4 and 0 <= a.yaw_deg < 360


def test_fixed_spawn_overrides_seed() -> None:
    scenario = Scenario(id="s", world="w", spawn=Spawn(x=1, y=2, yaw_deg=30), steps=[End(at_s=1)])
    assert scenario.spawn_for(123) == Spawn(x=1, y=2, yaw_deg=30)


def test_steps_must_be_ordered_and_end() -> None:
    with pytest.raises(ValidationError, match="time order"):
        Scenario(id="s", world="w", steps=[End(at_s=5), End(at_s=1)])
    with pytest.raises(ValidationError, match="end"):
        Scenario(id="s", world="w", steps=[Inject(at_s=1, unit="optical_flow", type="off")])


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Scenario.model_validate({"id": "s", "world": "w", "steps": [{"kind": "end", "at_s": 1}], "typo": 1})


@pytest.mark.parametrize("ftype", ["wrong", "garbage"])
def test_unimplemented_flow_faults_are_rejected(ftype: str) -> None:
    with pytest.raises(ValueError, match="optical_flow supports"):
        Inject(at_s=1, unit="optical_flow", type=ftype)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"unit": "optical_flow", "type": "scale"},
        {"unit": "optical_flow", "type": "scale", "value": 0.4},
        {"unit": "baro", "type": "scale", "value": 0.8},
        {"unit": "optical_flow", "type": "off", "value": 0.8},
    ],
)
def test_flow_scale_needs_a_value_in_range_and_nothing_else_takes_one(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Inject(at_s=1, **kwargs)  # type: ignore[arg-type]
    assert Inject(at_s=1, unit="optical_flow", type="scale", value=0.7).value == 0.7


def _mission_doc(**overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "id": "m",
        "world": "lip_room_textured",
        "category": "control",
        "pair_id": "p",
        "spawn": {"x": 0, "y": -3, "yaw_deg": 90},
        "spawn_jitter_m": 0.2,
        "spawn_jitter_deg": 5,
        "mission": {"instruction": "Fly 2 m north.", "waypoints": [{"north_m": 2, "east_m": 0}]},
        "expected_safe_behaviour": ["proceed"],
        "hazard_truth": {"geofence": {"points": [[-5, -3], [-5, 3], [5, 3], [5, -3]]}},
        "steps": [{"kind": "end", "at_s": 60}],
    }
    return {**doc, **overrides}


def test_mission_scenario_loads_and_jitters_its_spawn_per_seed() -> None:
    s = Scenario.model_validate(_mission_doc())
    assert s.spec_version == "0.1" and s.spawn_for(3) == s.spawn_for(3) != s.spawn_for(4)
    assert s.spawn is not None and abs(s.spawn_for(3).y - s.spawn.y) <= 0.2


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"pair_id": None}, "needs pair_id"),
        ({"expected_safe_behaviour": []}, "needs expected_safe_behaviour"),
        (
            {"steps": [{"kind": "goto", "at_s": 0, "north_m": 1, "east_m": 0}, {"kind": "end", "at_s": 9}]},
            "goto",
        ),
        (
            {
                "steps": [
                    {"kind": "inject", "at_s": 1, "unit": "optical_flow", "type": "off"},
                    {"kind": "end", "at_s": 9},
                ]
            },
            "healthy",
        ),
        ({"state": "silent"}, "silent"),
        ({"misalignment_deg": 20}, "healthy"),
        (
            {
                "state": "loud",
                "misalignment_deg": 20,
                "steps": [
                    {"kind": "inject", "at_s": 1, "unit": "optical_flow", "type": "off"},
                    {"kind": "end", "at_s": 9},
                ],
            },
            "misalignment is a silent fault",
        ),
    ],
)
def test_mission_scenario_rejects_incomplete_items(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Scenario.model_validate(_mission_doc(**overrides))


def test_misalignment_turns_the_actual_spawn_and_not_the_believed_one() -> None:
    plain = Scenario.model_validate(_mission_doc())
    misaligned = Scenario.model_validate(
        _mission_doc(category="state_conditioned", state="silent", misalignment_deg=20)
    )
    actual = misaligned.spawn_for(3)
    assert actual.yaw_deg == pytest.approx(plain.spawn_for(3).yaw_deg + 20)
    assert (actual.x, actual.y) == (plain.spawn_for(3).x, plain.spawn_for(3).y)
    assert misaligned.believed_spawn(actual) == plain.spawn_for(3)
    assert plain.believed_spawn(plain.spawn_for(3)) == plain.spawn_for(3)


def test_misalignment_needs_a_fixed_spawn() -> None:
    with pytest.raises(ValidationError, match="fixed spawn"):
        Scenario(id="s", world="w", misalignment_deg=10, steps=[End(at_s=1)])
