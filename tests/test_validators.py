from pathlib import Path

import pytest

from lost_in_place import geometry
from lost_in_place.planning import Action, EstimatorHealth, Observation
from lost_in_place.scenario import load_scenario
from lost_in_place.validators import (
    EstimatorHealthValidator,
    GeometricValidator,
    HazardMap,
    OracleValidator,
    Truth,
    hazard_in_truth,
    make_validator,
    rejection_check,
)

PILOT = Path(__file__).parents[1] / "scenarios" / "pilot"
CROSS = load_scenario(
    PILOT / "cross_healthy.yaml"
)  # spawn truth (-3.5, 0) facing north; shelf from 3.6 m north
assert CROSS.hazard_truth is not None and CROSS.spawn is not None
TRUTH_MAP = HazardMap.truth(CROSS.hazard_truth)
HEALTHY = EstimatorHealth(
    xy_valid=True, eph_m=0.1, flow_quality=180, innovation_check_failing=False, heading_reference=False
)


def _obs(
    north: float = 0.0, east: float = 0.0, health: EstimatorHealth | None = HEALTHY, battery: float = 90
) -> Observation:
    return Observation(
        goal="g",
        north_m=north,
        east_m=east,
        altitude_m=1.5,
        heading_deg=0,
        battery_pct=battery,
        health=health,
    )


def _move(north: float, east: float) -> Action:
    return Action("move", {"north_m": north, "east_m": east})


def _truth(north: float, east: float) -> Truth:
    assert CROSS.spawn is not None
    origin, heading = geometry.spawn_pose(CROSS.spawn.x, CROSS.spawn.y, CROSS.spawn.yaw_deg)
    return Truth(north_m=north, east_m=east, origin=origin, heading_rad=heading)


def test_hazard_map_moves_into_the_takeoff_frame() -> None:
    assert CROSS.spawn is not None
    estimated = TRUTH_MAP.in_takeoff_frame(CROSS.spawn)
    # The shelf starts 3.6 m north in truth, 7.1 m ahead of a spawn at -3.5 facing north.
    ((_, shelf),) = estimated.keep_out
    assert min(p[0] for p in shelf) == pytest.approx(7.1)


def test_geometric_checks_the_estimated_frame() -> None:
    geometric = make_validator("geometric", CROSS)
    assert geometric.check(_move(6.0, 0.0), _obs()).ok
    blocked = geometric.check(_move(7.5, 0.0), _obs())
    assert not blocked.ok and "shelf" in blocked.reason
    assert not geometric.check(_move(0.0, 3.0), _obs()).ok  # 3 m east of the take-off point is the wall
    assert geometric.check(Action("land"), _obs(battery=5)).ok


@pytest.mark.parametrize(
    ("action", "obs", "reason"),
    [
        (Action("take_off", {"altitude_m": 3.0}), _obs(), "altitude"),
        (Action("hold", {"seconds": 60.0}), _obs(), "hold longer"),
        (_move(1.0, 0.0), _obs(battery=10), "battery"),
        (_move(-3.0, 0.0), _obs(north=6.0), ""),  # 9 m back is within the room, but too far in one move
    ],
)
def test_dexi_envelope(action: Action, obs: Observation, reason: str) -> None:
    verdict = GeometricValidator(TRUTH_MAP, None).check(action, obs)
    assert not verdict.ok and reason in verdict.reason


@pytest.mark.parametrize(
    ("health", "reason"),
    [
        (EstimatorHealth(False, 0.1, 180, False, False), "invalid"),
        (EstimatorHealth(True, 1.5, 180, False, False), "eph"),
        (EstimatorHealth(True, 0.1, 180, True, False), "innovation"),
        (EstimatorHealth(True, 0.1, 30, False, False), "flow quality"),
    ],
)
def test_estimator_health_rejects_moves_on_bad_health(health: EstimatorHealth, reason: str) -> None:
    validator = make_validator("estimator_health", CROSS)
    assert isinstance(validator, EstimatorHealthValidator)
    verdict = validator.check(_move(1.0, 0.0), _obs(health=health))
    assert not verdict.ok and reason in verdict.reason
    assert validator.check(Action("land"), _obs(health=health)).ok
    assert validator.check(_move(1.0, 0.0), _obs()).ok


def test_silent_drift_blinds_the_geometric_validator_but_not_the_oracle() -> None:
    """The H2a mechanism: the estimate says 2 m from take-off, but the vehicle is already at the shelf."""
    geometric, oracle = make_validator("geometric", CROSS), make_validator("oracle", CROSS)
    assert isinstance(oracle, OracleValidator)
    move, believed = _move(6.0, 0.0), _obs(north=2.0)
    beside_the_target = _truth(3.0, 0.0)
    assert geometric.check(move, believed, beside_the_target).ok
    assert (
        hazard_in_truth(move, beside_the_target, TRUTH_MAP) == ""
    )  # the commanded target itself is safe ...
    drifted = _truth(4.6, 0.0)
    verdict = oracle.check(move, believed, drifted)  # ... but not from where the vehicle really is
    assert not verdict.ok and "shelf" in verdict.reason
    assert not oracle.check(move, believed, None).ok


def test_every_rejection_reason_names_its_check() -> None:
    geometric, health = make_validator("geometric", CROSS), make_validator("estimator_health", CROSS)
    h = EstimatorHealth

    cases = [
        (geometric.check(_move(7.5, 0.0), _obs()), "hazard_map"),
        (geometric.check(_move(0.0, 3.0), _obs()), "hazard_map"),
        (geometric.check(_move(-3.0, 0.0), _obs(north=6.0)), "envelope"),
        (geometric.check(Action("take_off", {"altitude_m": 3.0}), _obs()), "envelope"),
        (geometric.check(Action("hold", {"seconds": 60.0}), _obs()), "envelope"),
        (geometric.check(_move(1.0, 0.0), _obs(battery=10)), "envelope"),
        (health.check(_move(1.0, 0.0), _obs(health=None)), "health"),
        (health.check(_move(1.0, 0.0), _obs(health=h(False, 0.1, 180, False, False))), "health"),
        (health.check(_move(1.0, 0.0), _obs(health=h(True, 1.5, 180, False, False))), "health"),
        (health.check(_move(1.0, 0.0), _obs(health=h(True, 0.1, 180, True, False))), "health"),
        (health.check(_move(1.0, 0.0), _obs(health=h(True, 0.1, 30, False, False))), "health"),
    ]
    for verdict, check in cases:
        assert not verdict.ok
        assert rejection_check(verdict.reason) == check, verdict.reason
    assert rejection_check("no ground truth") == "other"


def test_only_moves_can_be_hazardous() -> None:
    assert hazard_in_truth(Action("hold", {"seconds": 5.0}), _truth(4.6, 0.0), TRUTH_MAP) == ""


def test_unknown_validator() -> None:
    with pytest.raises(LookupError):
        make_validator("psychic", CROSS)
