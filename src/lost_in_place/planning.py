"""Interfaces between the benchmark and LLM planners or validators.

Planners plug in through the `lost_in_place.planners` entry-point group, so a private
project can provide one without this repo depending on it:

    # in the planner project's pyproject.toml
    [project.entry-points."lost_in_place.planners"]
    dexi = "dexi_planner.benchmark_adapter:make_planner"

The factory returns an object implementing `Planner`. Validators are separate on purpose:
the benchmark decides which validator arm checks a proposal, so a planner only proposes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Literal, Protocol, runtime_checkable

ActionName = Literal["take_off", "move", "rotate", "hold", "land", "refuse"]
ENTRY_POINT_GROUP = "lost_in_place.planners"


@dataclass(frozen=True)
class EstimatorHealth:
    """What PX4 reports about its own estimate; shown to the planner only at observation level O1."""

    xy_valid: bool
    eph_m: float
    flow_quality: float
    innovation_check_failing: bool
    heading_reference: bool  # always False on a vehicle without a compass


@dataclass(frozen=True)
class Observation:
    goal: str
    north_m: float
    east_m: float
    altitude_m: float
    heading_deg: float
    battery_pct: float
    history: tuple[str, ...] = ()
    health: EstimatorHealth | None = None  # None at observation level O0


@dataclass(frozen=True)
class Action:
    name: ActionName
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@runtime_checkable
class Planner(Protocol):
    async def propose(self, observation: Observation) -> Action: ...


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""


@runtime_checkable
class Validator(Protocol):
    def check(self, action: Action, observation: Observation) -> Verdict: ...


class NoValidator:
    """The `none` arm: every proposal passes."""

    def check(self, action: Action, observation: Observation) -> Verdict:
        return Verdict(ok=True)


def available_planners() -> list[str]:
    return sorted(ep.name for ep in entry_points(group=ENTRY_POINT_GROUP))


def load_planner(name: str, **options: Any) -> Planner:
    matches = [ep for ep in entry_points(group=ENTRY_POINT_GROUP) if ep.name == name]
    if not matches:
        raise LookupError(f"no planner '{name}'; installed: {', '.join(available_planners()) or 'none'}")
    planner = matches[0].load()(**options)
    if not isinstance(planner, Planner):
        raise TypeError(f"planner '{name}' does not implement propose(observation)")
    return planner
