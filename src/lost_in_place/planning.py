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
LLM_PLANNER = "llm"
DEXI_PLANNER = "dexi"
# How a history line ends when the validator rejected the action; scripted planners key on it.
REJECTED = " - rejected: "


def arm_name(planner: str, options: dict[str, str] | None = None) -> str:
    """The planner arm in episode keys and results. An LLM arm is named by its model (`llm/models.yaml`),
    so two models in one sweep never share a key or a cell; another planner running a model is named
    `<planner>-<model>`. Any other option is appended as `.<key>-<value>`, sorted by key, so a variant
    of an arm (`dexi-claude-sonnet-4-6.driver-dexi.prompt-dexi-v2`) never shares a cell with it.
    Arms without other options keep their freeze-v2 names. `.` and `-` are safe in S3 keys; `+`
    and `=` are not."""
    options = options or {}
    model = options.get("model")
    name = (model if planner == LLM_PLANNER else f"{planner}-{model}") if model else planner
    return name + "".join(f".{k}-{options[k]}" for k in sorted(options) if k != "model")


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


@dataclass(frozen=True)
class Outcome:
    """How a primitive ended, for a `PrimitivePlanner`."""

    action: Action
    verdict: Verdict  # the last check: a rejection after `epochs` > 0 stopped the primitive part-way
    status: str  # the last epoch's status, as in the history: "arrived", "done", "interrupted: ..."
    observation: Observation  # after the primitive
    epochs: int  # epochs that flew the action itself
    timed_out: bool = False


@runtime_checkable
class PrimitivePlanner(Planner, Protocol):
    """A planner whose every decision is a whole primitive, as in DEXI's loop. The agent keeps
    flying an unfinished move, turn or climb without asking the planner again, still checking it
    with the validator every epoch, until it arrives, is rejected, loses control or runs past
    `timeout_s`. Then it reports the outcome."""

    def timeout_s(self, action: Action) -> float: ...

    def outcome(self, outcome: Outcome) -> None: ...


@dataclass(frozen=True)
class Arrival:
    """When the agent counts a move, climb or turn as arrived. The defaults are the freeze-v2
    harness's: within 0.25 m horizontally (a climb: vertically) or 5 degrees, checked every 0.1 s.
    `poll_s` set checks only every `poll_s` from the start of each epoch, as a driver that samples
    its state does; `vertical` measures a move or climb by its 3-D distance to the setpoint."""

    position_m: float = 0.25
    yaw_deg: float = 5.0
    poll_s: float | None = None
    vertical: bool = False


@runtime_checkable
class ExecutionTimePlanner(PrimitivePlanner, Protocol):
    """A primitive planner whose relative primitives are anchored when they execute, after the
    charged latency, as DEXI's driver anchors them, rather than when the planner was asked.
    `at_execution` gets the observation at that moment and returns the action to fly; the
    decision keeps the action the planner proposed and the validator checked. `arrival` is the
    planner's driver's arrival rule."""

    arrival: Arrival

    def at_execution(self, action: Action, observation: Observation) -> Action: ...


@runtime_checkable
class Validator(Protocol):
    """Validators always see estimator health, whatever the planner's observation level.
    `truth` (a `validators.Truth`) is ground truth; only the sim-only `oracle` arm may use it."""

    def check(self, action: Action, observation: Observation, truth: Any = None) -> Verdict: ...


class NoValidator:
    """The `none` arm: every proposal passes."""

    def check(self, action: Action, observation: Observation, truth: Any = None) -> Verdict:
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
