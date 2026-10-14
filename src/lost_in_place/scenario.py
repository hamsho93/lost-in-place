"""Scenario files: what world to fly in, where to spawn, and what happens when.

Times are seconds of simulation time after offboard start. Positions are metres in the
EKF's local frame (north/east as the estimator sees them), which on a no-compass vehicle
is the frame defined by the nose direction at boot.

A scenario with a `mission` is a benchmark item (spec v0.1): a planner flies the instruction,
and `hazard_truth` says, in the truth frame (see `geometry`), where it must not go. Without a
mission, the `goto` steps fly a scripted path.
"""

from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Spawn(_Strict):
    x: float = 0.0
    y: float = 0.0
    yaw_deg: float = Field(0.0, description="Gazebo ENU yaw; EKF north ends up at 90 - yaw_deg")

    @classmethod
    def from_seed(cls, seed: int, half_extent_m: float = 4.0) -> Spawn:
        rng = random.Random(seed)
        return cls(
            x=rng.uniform(-half_extent_m, half_extent_m),
            y=rng.uniform(-half_extent_m, half_extent_m),
            yaw_deg=math.degrees(rng.uniform(0.0, 2.0 * math.pi)),
        )


class Goto(_Strict):
    kind: Literal["goto"] = "goto"
    at_s: float = Field(ge=0)
    north_m: float
    east_m: float


FaultUnit = Literal["optical_flow", "distance_sensor", "gyro", "accel", "baro", "battery"]
FaultType = Literal["ok", "off", "stuck", "garbage", "wrong", "scale"]


# px4/patches/0002 implements only ok, off and stuck for optical flow; its "wrong" is quality 0,
# the same loud fault as "off". The silent scale error the experiment design calls "wrong" is
# "scale" here: PX4's SENS_FLOW_SCALE, set mid-flight, multiplies the flow at full quality.
OPTICAL_FLOW_FAULTS = ("ok", "off", "stuck", "scale")
FLOW_SCALE_PARAM = "SENS_FLOW_SCALE"
FLOW_SCALE_RANGE = (0.5, 1.5)


class Inject(_Strict):
    kind: Literal["inject"] = "inject"
    at_s: float = Field(ge=0)
    unit: FaultUnit
    type: FaultType
    value: float | None = Field(None, description="The flow scale factor k, for type 'scale' only")

    @model_validator(mode="after")
    def _implemented(self) -> Inject:
        if self.unit == "optical_flow" and self.type not in OPTICAL_FLOW_FAULTS:
            raise ValueError(f"optical_flow supports {', '.join(OPTICAL_FLOW_FAULTS)}, not {self.type!r}")
        if self.type == "scale":
            lo, hi = FLOW_SCALE_RANGE
            if self.unit != "optical_flow" or self.value is None or not lo <= self.value <= hi:
                raise ValueError(f"'scale' is an optical_flow fault with a value from {lo} to {hi}")
        elif self.value is not None:
            raise ValueError(f"only 'scale' takes a value, not {self.type!r}")
        return self


class SetParam(_Strict):
    kind: Literal["param"] = "param"
    at_s: float = Field(ge=0)
    name: str
    value: float
    type: Literal["float", "int"] = Field("float", description="The PX4 parameter's type")

    @model_validator(mode="after")
    def _whole_if_int(self) -> SetParam:
        if self.type == "int" and not float(self.value).is_integer():
            raise ValueError(f"{self.name} is an int parameter, not {self.value}")
        return self


class End(_Strict):
    kind: Literal["end"] = "end"
    at_s: float = Field(ge=0)


Step = Annotated[Goto | Inject | SetParam | End, Field(discriminator="kind")]


SPEC_VERSION = "0.1"

Category = Literal["state_conditioned", "control", "degraded_safe", "instruction_hazard"]
State = Literal["healthy", "loud", "silent", "degraded_safe"]
SafeBehaviour = Literal["proceed", "hold", "land", "refuse"]


class Polygon(_Strict):
    name: str = ""
    points: list[tuple[float, float]] = Field(min_length=3, description="(north, east) in the truth frame")


class HazardTruth(_Strict):
    geofence: Polygon = Field(description="The vehicle must stay inside this")
    keep_out: list[Polygon] = []
    buffer_m: float = Field(
        0.3, ge=0, description="Closer than this to a keep-out or the geofence edge is a violation"
    )


class Waypoint(_Strict):
    north_m: float
    east_m: float


class Mission(_Strict):
    instruction: str = Field(min_length=1, description="What the planner is told to do")
    waypoints: list[Waypoint] = Field(
        min_length=1, description="The instruction's targets in the take-off frame, in order"
    )
    hold_s: float = Field(10.0, ge=0, description="Hold at the last waypoint before landing")
    planner_calls: int = Field(40, gt=0, description="Planner call budget")
    fault_anchor: Literal["arm", "first_move", "first_move_target"] = Field(
        "first_move",
        description="What fault step times count from: arming, the first executed move, or the estimate"
        " coming within `fault_anchor_m` of the first executed move's target. 'end' always counts"
        " from arming",
    )
    fault_anchor_m: float | None = Field(
        None, gt=0, description="For 'first_move_target': how close to the target the clock starts"
    )

    @model_validator(mode="after")
    def _anchor_distance(self) -> Mission:
        if (self.fault_anchor == "first_move_target") != (self.fault_anchor_m is not None):
            raise ValueError("fault_anchor_m goes with fault_anchor 'first_move_target', and only with it")
        return self


class Scenario(_Strict):
    id: str
    world: str
    altitude_m: float = Field(1.5, gt=0)
    spawn: Spawn | None = Field(None, description="Fixed spawn; if omitted it is drawn from the seed")
    spawn_jitter_m: float = Field(0.0, ge=0, description="Per-seed uniform jitter of a fixed spawn")
    spawn_jitter_deg: float = Field(0.0, ge=0)
    misalignment_deg: float = Field(
        0.0,
        description="Silent frame misalignment: the vehicle is placed this far from the nominal yaw,"
        " which the validators and the instruction still assume",
    )
    steps: list[Step]
    notes: str = ""
    spec_version: str = SPEC_VERSION
    category: Category | None = None
    state: State = "healthy"
    pair_id: str | None = Field(None, description="Links a scenario to its matched controls")
    mission: Mission | None = None
    expected_safe_behaviour: list[SafeBehaviour] = []
    hazard_truth: HazardTruth | None = None

    @model_validator(mode="after")
    def _ordered_and_terminated(self) -> Scenario:
        times = [s.at_s for s in self.steps]
        if times != sorted(times):
            raise ValueError("steps must be in time order")
        if not self.steps or not isinstance(self.steps[-1], End):
            raise ValueError("the last step must be an 'end' step")
        return self

    @model_validator(mode="after")
    def _benchmark_item(self) -> Scenario:
        if self.mission is None:
            return self
        missing = [
            name for name in ("spawn", "category", "pair_id", "hazard_truth") if getattr(self, name) is None
        ]
        if not self.expected_safe_behaviour:
            missing.append("expected_safe_behaviour")
        if missing:
            raise ValueError(f"a mission scenario needs {', '.join(missing)}")
        if any(isinstance(s, Goto) for s in self.steps):
            raise ValueError("a mission scenario is flown by the planner, so it can't have 'goto' steps")
        faults = len([s for s in self.steps if isinstance(s, Inject) and s.type != "ok"])
        faults += bool(self.misalignment_deg)
        if (self.state == "healthy") == bool(faults):
            raise ValueError(f"state {self.state!r} doesn't match {faults} fault(s)")
        if self.misalignment_deg and self.state != "silent":
            raise ValueError("frame misalignment is a silent fault")
        return self

    @model_validator(mode="after")
    def _misaligned_from_a_known_spawn(self) -> Scenario:
        if self.misalignment_deg and self.spawn is None:
            raise ValueError("misalignment_deg needs a fixed spawn to be misaligned from")
        return self

    def spawn_for(self, seed: int) -> Spawn:
        """Where the vehicle actually starts, misalignment included."""
        if self.spawn is None:
            return Spawn.from_seed(seed)
        if not (self.spawn_jitter_m or self.spawn_jitter_deg or self.misalignment_deg):
            return self.spawn
        rng = random.Random(seed)
        return Spawn(
            x=self.spawn.x + rng.uniform(-self.spawn_jitter_m, self.spawn_jitter_m),
            y=self.spawn.y + rng.uniform(-self.spawn_jitter_m, self.spawn_jitter_m),
            yaw_deg=self.spawn.yaw_deg
            + rng.uniform(-self.spawn_jitter_deg, self.spawn_jitter_deg)
            + self.misalignment_deg,
        )

    def believed_spawn(self, actual: Spawn) -> Spawn:
        """The spawn the instruction was written for: the actual one without the misalignment."""
        return actual.model_copy(update={"yaw_deg": actual.yaw_deg - self.misalignment_deg})


def load_scenario(path: str | Path) -> Scenario:
    data = yaml.safe_load(Path(path).read_text())
    return Scenario.model_validate(data)
