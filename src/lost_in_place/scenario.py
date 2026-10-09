"""Scenario files: what world to fly in, where to spawn, and what happens when.

Times are seconds of simulation time after offboard start. Positions are metres in the
EKF's local frame (north/east as the estimator sees them), which on a no-compass vehicle
is the frame defined by the nose direction at boot.
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
FaultType = Literal["ok", "off", "stuck", "garbage", "wrong"]


class Inject(_Strict):
    kind: Literal["inject"] = "inject"
    at_s: float = Field(ge=0)
    unit: FaultUnit
    type: FaultType


class SetParam(_Strict):
    kind: Literal["param"] = "param"
    at_s: float = Field(ge=0)
    name: str
    value: float


class End(_Strict):
    kind: Literal["end"] = "end"
    at_s: float = Field(ge=0)


Step = Annotated[Goto | Inject | SetParam | End, Field(discriminator="kind")]


class Scenario(_Strict):
    id: str
    world: str
    altitude_m: float = Field(1.5, gt=0)
    spawn: Spawn | None = Field(None, description="Fixed spawn; if omitted it is drawn from the seed")
    steps: list[Step]
    notes: str = ""

    @model_validator(mode="after")
    def _ordered_and_terminated(self) -> Scenario:
        times = [s.at_s for s in self.steps]
        if times != sorted(times):
            raise ValueError("steps must be in time order")
        if not self.steps or not isinstance(self.steps[-1], End):
            raise ValueError("the last step must be an 'end' step")
        return self

    def spawn_for(self, seed: int) -> Spawn:
        return self.spawn if self.spawn is not None else Spawn.from_seed(seed)


def load_scenario(path: str | Path) -> Scenario:
    data = yaml.safe_load(Path(path).read_text())
    return Scenario.model_validate(data)
