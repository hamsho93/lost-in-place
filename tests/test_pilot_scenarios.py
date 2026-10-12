import itertools
from collections import defaultdict
from pathlib import Path

import pytest

from lost_in_place import geometry
from lost_in_place.scenario import Scenario, Spawn, load_scenario
from lost_in_place.worlds import ROOMS

PILOT = sorted((Path(__file__).parents[1] / "scenarios" / "pilot").glob("*.yaml"))
SCENARIOS = [load_scenario(p) for p in PILOT]


def _path_in_truth(s: Scenario, spawn: Spawn) -> list[geometry.Point]:
    assert s.mission is not None
    origin, heading = geometry.spawn_pose(spawn.x, spawn.y, spawn.yaw_deg)
    corners = [(0.0, 0.0)] + [(w.north_m, w.east_m) for w in s.mission.waypoints]
    truth = [geometry.takeoff_to_truth(p, origin, heading) for p in corners]
    return [p for a, b in itertools.pairwise(truth) for p in geometry.segment_points(a, b)]


def _extreme_spawns(s: Scenario) -> list[Spawn]:
    assert s.spawn is not None
    m, d = s.spawn_jitter_m, s.spawn_jitter_deg
    return [
        Spawn(x=s.spawn.x + dx, y=s.spawn.y + dy, yaw_deg=s.spawn.yaw_deg + dyaw)
        for dx, dy, dyaw in itertools.product((-m, m), (-m, m), (-d, 0.0, d))
    ]


def test_pilot_set_is_complete() -> None:
    assert len(SCENARIOS) >= 9
    by_pair: dict[str, set[str]] = defaultdict(set)
    for s in SCENARIOS:
        assert s.pair_id is not None
        by_pair[s.pair_id].add(s.state)
    assert all(states == {"healthy", "loud", "silent"} for states in by_pair.values())


@pytest.mark.parametrize("s", SCENARIOS, ids=lambda s: s.id)
def test_matched_scenarios_differ_only_in_faults(s: Scenario) -> None:
    control = next(c for c in SCENARIOS if c.pair_id == s.pair_id and c.state == "healthy")
    ignore = {"id", "state", "category", "steps", "expected_safe_behaviour"}
    assert s.model_dump(exclude=ignore) == control.model_dump(exclude=ignore)


@pytest.mark.parametrize("s", [s for s in SCENARIOS if s.state == "healthy"], ids=lambda s: s.id)
def test_healthy_path_is_clear_of_hazards_at_any_jitter(s: Scenario) -> None:
    assert s.hazard_truth is not None
    room = ROOMS[s.world]
    assert (
        s.hazard_truth.geofence.points == [list(p) for p in room.interior_ne()]
        or [tuple(p) for p in s.hazard_truth.geofence.points] == room.interior_ne()
    )
    clearance = 2 * s.hazard_truth.buffer_m
    for spawn in _extreme_spawns(s):
        for p in _path_in_truth(s, spawn):
            assert geometry.inside_with_margin(s.hazard_truth.geofence.points, p, clearance), (spawn, p)
            for zone in s.hazard_truth.keep_out:
                assert not geometry.touches(zone.points, p, clearance), (zone.name, spawn, p)
