from importlib.metadata import EntryPoint
from typing import Any

import pytest

from lost_in_place import planning
from lost_in_place.planning import Action, NoValidator, Observation, Planner, load_planner


class HoldPlanner:
    async def propose(self, observation: Observation) -> Action:
        return Action(name="hold", reason="test")


def make_hold_planner(**_: Any) -> HoldPlanner:
    return HoldPlanner()


OBS = Observation(goal="hover", north_m=0, east_m=0, altitude_m=1.5, heading_deg=0, battery_pct=90)


def _fake_entry_points(group: str) -> list[EntryPoint]:
    assert group == planning.ENTRY_POINT_GROUP
    return [EntryPoint(name="hold", value=f"{__name__}:make_hold_planner", group=group)]


def test_load_planner_from_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(planning, "entry_points", _fake_entry_points)
    planner = load_planner("hold")
    assert isinstance(planner, Planner)
    assert planning.available_planners() == ["hold"]


def test_unknown_planner_lists_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(planning, "entry_points", _fake_entry_points)
    with pytest.raises(LookupError, match="installed: hold"):
        load_planner("missing")


def test_no_validator_passes_everything() -> None:
    assert NoValidator().check(Action(name="move", args={"north_m": 100}), OBS).ok
