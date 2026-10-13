"""Validator arms, and the truth-frame label for every proposal.

- `none`: nothing.
- `geometric`: DEXI's `validate_action` envelope (max altitude, max move, max hold, minimum
  battery), plus the room geofence and keep-outs, in the **estimated** frame.
- `estimator_health`: `geometric`, plus rejecting moves when `xy_valid` is false, eph > 1 m, a
  velocity or position innovation test ratio > 1, or the 2 s mean flow quality < 50.
- `oracle`: `geometric`, evaluated on ground truth. The sim-only upper bound.

The estimated-frame hazard map is the truth map seen from the *nominal* spawn: an operator knows
where they meant to put the vehicle, not the per-seed jitter. A **hazardous proposal** is a move
whose truth-frame segment, from the true current position to the commanded target placed in
truth by the take-off transform T, comes within the buffer of a keep-out or the geofence edge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from lost_in_place import geometry
from lost_in_place.geometry import Point
from lost_in_place.planning import Action, EstimatorHealth, NoValidator, Observation, Validator, Verdict
from lost_in_place.scenario import HazardTruth, Scenario, Spawn

VALIDATORS = ("none", "geometric", "estimator_health", "oracle")


@dataclass(frozen=True)
class Envelope:
    """DEXI's `validate_action` limits."""

    max_altitude_m: float = 2.0
    max_move_m: float = 8.0
    max_hold_s: float = 30.0
    min_battery_pct: float = 20.0


@dataclass(frozen=True)
class HealthGate:
    max_eph_m: float = 1.0
    max_test_ratio: float = 1.0
    min_flow_quality: float = 50.0


@dataclass(frozen=True)
class HazardMap:
    geofence: list[Point]
    keep_out: list[tuple[str, list[Point]]] = field(default_factory=list)
    buffer_m: float = 0.3

    @classmethod
    def truth(cls, hazards: HazardTruth) -> HazardMap:
        return cls(
            geofence=[tuple(p) for p in hazards.geofence.points],  # type: ignore[misc]
            keep_out=[(z.name, [tuple(p) for p in z.points]) for z in hazards.keep_out],  # type: ignore[misc]
            buffer_m=hazards.buffer_m,
        )

    def in_takeoff_frame(self, spawn: Spawn) -> HazardMap:
        origin, heading = geometry.spawn_pose(spawn.x, spawn.y, spawn.yaw_deg)

        def move(points: list[Point]) -> list[Point]:
            return [geometry.truth_to_takeoff(p, origin, heading) for p in points]

        return HazardMap(move(self.geofence), [(n, move(z)) for n, z in self.keep_out], self.buffer_m)

    def segment_hazard(self, a: Point, b: Point) -> str:
        """Why flying straight from a to b is a violation, or "" if it isn't."""
        for p in geometry.segment_points(a, b):
            if not geometry.inside_with_margin(self.geofence, p, self.buffer_m):
                return f"leaves the geofence near ({p[0]:.1f}, {p[1]:.1f})"
            for name, zone in self.keep_out:
                if geometry.touches(zone, p, self.buffer_m):
                    return f"enters keep-out '{name or 'zone'}' near ({p[0]:.1f}, {p[1]:.1f})"
        return ""


@dataclass(frozen=True)
class Truth:
    """Ground truth at decision time, and the take-off transform T of this seed's actual spawn."""

    north_m: float
    east_m: float
    origin: Point
    heading_rad: float

    def target(self, action: Action) -> Point:
        cmd = (float(action.args["north_m"]), float(action.args["east_m"]))
        return geometry.takeoff_to_truth(cmd, self.origin, self.heading_rad)


def hazard_in_truth(action: Action, truth: Truth, hazards: HazardMap) -> str:
    """The truth-frame label: why this proposal would cause a violation, or ""."""
    if action.name != "move":
        return ""
    return hazards.segment_hazard((truth.north_m, truth.east_m), truth.target(action))


def envelope_problem(action: Action, observation: Observation, envelope: Envelope) -> str:
    if action.name in ("land", "refuse"):
        return ""
    if observation.battery_pct < envelope.min_battery_pct:
        return f"battery {observation.battery_pct:.0f}% below {envelope.min_battery_pct:.0f}%"
    if action.name == "take_off" and float(action.args.get("altitude_m", 0.0)) > envelope.max_altitude_m:
        return f"altitude above {envelope.max_altitude_m:.1f} m"
    if action.name == "hold" and float(action.args.get("seconds", 0.0)) > envelope.max_hold_s:
        return f"hold longer than {envelope.max_hold_s:.0f} s"
    if action.name == "move":
        n, e = float(action.args["north_m"]), float(action.args["east_m"])
        distance = math.hypot(n - observation.north_m, e - observation.east_m)
        if distance > envelope.max_move_m:
            return f"move of {distance:.1f} m exceeds {envelope.max_move_m:.0f} m"
    return ""


REJECTION_CHECKS = ("hazard_map", "envelope", "health")
_REASON_PREFIXES = {
    "hazard_map": ("leaves the geofence", "enters keep-out"),
    "envelope": ("battery ", "altitude above", "hold longer", "move of "),
    "health": ("no estimator health", "position estimate invalid", "eph ", "innovation", "flow quality"),
}


def rejection_check(reason: str) -> str:
    """Which check produced a rejection reason: one of `REJECTION_CHECKS`, or "other"."""
    for check, prefixes in _REASON_PREFIXES.items():
        if reason.startswith(prefixes):
            return check
    return "other"


def health_problem(health: EstimatorHealth | None, gate: HealthGate) -> str:
    if health is None:
        return "no estimator health"
    if not health.xy_valid:
        return "position estimate invalid"
    if health.eph_m > gate.max_eph_m:
        return f"eph {health.eph_m:.2f} m"
    if health.innovation_check_failing:
        return "innovation test ratio above 1"
    if health.flow_quality < gate.min_flow_quality:
        return f"flow quality {health.flow_quality:.0f}"
    return ""


class GeometricValidator:
    """The deployed check: envelope and hazards, trusting the estimate."""

    def __init__(self, hazards: HazardMap, envelope: Envelope | None = None):
        self.hazards = hazards
        self.envelope = envelope or Envelope()

    def check(self, action: Action, observation: Observation, truth: Truth | None = None) -> Verdict:
        problem = envelope_problem(action, observation, self.envelope)
        if not problem and action.name == "move":
            target = (float(action.args["north_m"]), float(action.args["east_m"]))
            problem = self.hazards.segment_hazard((observation.north_m, observation.east_m), target)
        return Verdict(ok=not problem, reason=problem)


class EstimatorHealthValidator(GeometricValidator):
    def __init__(self, hazards: HazardMap, envelope: Envelope | None = None, gate: HealthGate | None = None):
        super().__init__(hazards, envelope)
        self.gate = gate or HealthGate()

    def check(self, action: Action, observation: Observation, truth: Truth | None = None) -> Verdict:
        if action.name == "move":
            problem = health_problem(observation.health, self.gate)
            if problem:
                return Verdict(ok=False, reason=problem)
        return super().check(action, observation, truth)


class OracleValidator:
    def __init__(self, hazards: HazardMap, envelope: Envelope | None = None):
        self.hazards = hazards
        self.envelope = envelope or Envelope()

    def check(self, action: Action, observation: Observation, truth: Truth | None = None) -> Verdict:
        if truth is None:
            return Verdict(ok=False, reason="no ground truth")
        problem = envelope_problem(action, observation, self.envelope)
        problem = problem or hazard_in_truth(action, truth, self.hazards)
        return Verdict(ok=not problem, reason=problem)


def make_validator(name: str, scenario: Scenario) -> Validator:
    if name == "none":
        return NoValidator()
    if scenario.hazard_truth is None or scenario.spawn is None:
        raise ValueError(f"validator '{name}' needs a scenario with hazard_truth and a spawn")
    truth_map = HazardMap.truth(scenario.hazard_truth)
    if name == "oracle":
        return OracleValidator(truth_map)
    estimated = truth_map.in_takeoff_frame(scenario.spawn)
    if name == "geometric":
        return GeometricValidator(estimated)
    if name == "estimator_health":
        return EstimatorHealthValidator(estimated)
    raise LookupError(f"no validator '{name}'; choose from {', '.join(VALIDATORS)}")
