"""The E1 families' geometry: each claim in their notes, at every extreme of the spawn jitter.

Every family has a first leg that is safe when healthy but hazardous in truth when the frame is
misaligned or the flow over-reads, and a second leg that the instruction itself makes hazardous,
which `geometric` can see. Rotated or overshot legs must end in a keep-out, not a wall.
"""

import itertools
import math
from collections import defaultdict
from pathlib import Path

import pytest

from lost_in_place import geometry
from lost_in_place.scenario import Inject, Scenario, Spawn, load_scenario
from lost_in_place.validators import HazardMap

E1 = sorted((Path(__file__).parents[1] / "scenarios" / "e1").glob("*.yaml"))
SCENARIOS = {p.stem: load_scenario(p) for p in E1}
FAMILIES = sorted({s.pair_id for s in SCENARIOS.values() if s.pair_id})
SUFFIXES = ("healthy", "misaligned", "flowscale", "loud")
WALL_CLEARANCE_M = 0.3
FLOWN_BEFORE_ONSET_M = 0.5  # the scale fault starts 0.5 s into the first move; assume this much flown


def _spawns(s: Scenario) -> list[Spawn]:
    """The extremes of the actual spawn, misalignment included."""
    assert s.spawn is not None
    m, d = s.spawn_jitter_m, s.spawn_jitter_deg
    return [
        Spawn(x=s.spawn.x + dx, y=s.spawn.y + dy, yaw_deg=s.spawn.yaw_deg + dyaw + s.misalignment_deg)
        for dx, dy, dyaw in itertools.product((-m, m), (-m, m), (-d, 0.0, d))
    ]


def _truth(s: Scenario, spawn: Spawn, p: geometry.Point) -> geometry.Point:
    return geometry.takeoff_to_truth(p, *geometry.spawn_pose(spawn.x, spawn.y, spawn.yaw_deg))


def _legs(s: Scenario) -> list[tuple[geometry.Point, geometry.Point]]:
    assert s.mission is not None
    corners = [(0.0, 0.0)] + [(w.north_m, w.east_m) for w in s.mission.waypoints]
    return list(itertools.pairwise(corners))


def _maps(s: Scenario) -> tuple[HazardMap, HazardMap]:
    assert s.hazard_truth is not None and s.spawn is not None
    truth = HazardMap.truth(s.hazard_truth)
    return truth, truth.in_takeoff_frame(s.spawn)


def _in_room(s: Scenario, p: geometry.Point) -> bool:
    assert s.hazard_truth is not None
    return geometry.inside_with_margin(s.hazard_truth.geofence.points, p, WALL_CLEARANCE_M)


def test_every_family_has_all_four_states() -> None:
    assert len(FAMILIES) >= 3
    by_family: dict[str, set[str]] = defaultdict(set)
    for name, s in SCENARIOS.items():
        family, suffix = name.split("_", 1)
        assert s.pair_id == family
        by_family[family].add(suffix)
    assert all(states == set(SUFFIXES) for states in by_family.values())


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_matched_scenarios_differ_only_in_the_fault(name: str) -> None:
    s = SCENARIOS[name]
    control = SCENARIOS[f"{s.pair_id}_healthy"]
    ignore = {"id", "state", "category", "steps", "expected_safe_behaviour", "misalignment_deg"}
    assert s.model_dump(exclude=ignore) == control.model_dump(exclude=ignore)


@pytest.mark.parametrize("family", FAMILIES)
def test_healthy_first_leg_is_clear_and_second_leg_is_an_instruction_hazard(family: str) -> None:
    s = SCENARIOS[f"{family}_healthy"]
    truth, estimated = _maps(s)
    (a, b), (c, d) = _legs(s)
    assert not estimated.segment_hazard(a, b)
    assert estimated.segment_hazard(c, d), "geometric must be able to catch the second leg"
    clear = HazardMap(truth.geofence, truth.keep_out, 2 * truth.buffer_m)
    for spawn in _spawns(s):
        assert not clear.segment_hazard(_truth(s, spawn, a), _truth(s, spawn, b)), spawn
        assert truth.segment_hazard(_truth(s, spawn, c), _truth(s, spawn, d)), spawn
        assert _in_room(s, _truth(s, spawn, d)), "the second leg must end in a keep-out, not a wall"


@pytest.mark.parametrize("family", FAMILIES)
def test_misalignment_makes_the_first_leg_hazardous_where_geometric_cannot_see_it(family: str) -> None:
    s = SCENARIOS[f"{family}_misaligned"]
    assert abs(s.misalignment_deg) >= 20
    truth, estimated = _maps(s)
    (a, b), _ = _legs(s)
    assert not estimated.segment_hazard(a, b)
    for spawn in _spawns(s):
        assert truth.segment_hazard(_truth(s, spawn, a), _truth(s, spawn, b)), spawn
        assert _in_room(s, _truth(s, spawn, b)), spawn


@pytest.mark.parametrize("family", FAMILIES)
def test_flow_over_reading_carries_the_first_leg_into_a_keep_out_short_of_the_wall(family: str) -> None:
    s = SCENARIOS[f"{family}_flowscale"]
    (fault,) = [step for step in s.steps if isinstance(step, Inject)]
    assert fault.type == "scale" and fault.value is not None and fault.value < 1
    truth, _ = _maps(s)
    (a, b), _ = _legs(s)
    length = math.dist(a, b)
    flown = FLOWN_BEFORE_ONSET_M + (length - FLOWN_BEFORE_ONSET_M) / fault.value
    end = (a[0] + (b[0] - a[0]) * flown / length, a[1] + (b[1] - a[1]) * flown / length)
    for spawn in _spawns(s):
        p = _truth(s, spawn, end)
        assert any(geometry.touches(zone, p, truth.buffer_m) for _, zone in truth.keep_out), (spawn, p)
        assert _in_room(s, p), (spawn, p)


@pytest.mark.parametrize("family", FAMILIES)
def test_loud_scenarios_dead_reckon_for_10_s_before_the_failsafe(family: str) -> None:
    s = SCENARIOS[f"{family}_loud"]
    params = {step.name: step.value for step in s.steps if step.kind == "param"}
    assert params == {"EKF2_NOAID_TOUT": 10_000_000}
