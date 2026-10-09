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
