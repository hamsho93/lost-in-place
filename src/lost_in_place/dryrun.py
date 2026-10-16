"""The kinematic dry run (freeze-v3 §5.2, U18): a real planner client flown against a kinematic double
instead of PX4, for the wording check that picks E5's instruction template (rule A-1).

The double:

- Each primitive moves the estimate, and truth with it, to its target at `SPEED_M_S` (a turn at
  `YAW_RATE_DEG_S`). It counts as arrived by DEXI's rule (`DEXI_ARRIVAL`: within 0.3 m in 3-D or
  8 degrees, sampled every 0.5 s), and the vehicle is then at the target, so no shortfall carries
  into the next relative move.
- Health is fixed and healthy (`HEALTH`). The planner sees it only at an O1 level; the run is at O0.
- `driver=dexi` (moves anchored when they execute), validator `none`, no charged latency. The
  model's temperature is the one its `models.yaml` entry pins, which must be 0.
- The loop is the agent's: an invalid proposal is a call, flown as a hold of one epoch; `land` and
  `refuse` end the episode, as do the scenario's time budget and planner-call budget.

Success: the episode ends with `land`, the valid holds just before it total at least `FINAL_HOLD_S`,
and the first of them begins within `GOAL_RADIUS_M` of the last waypoint.

The families are the retired b00-b59, re-worded from their waypoints under each template, and the
canaries k000-k005 (`families`): 66 per template.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from lost_in_place import families as fam
from lost_in_place.agent import MAX_HOLD_S, AgentConfig
from lost_in_place.episode import TAKEOFF_CHECK_SIM_S
from lost_in_place.llm import dexi
from lost_in_place.llm.clients import ChatClient, Completion, ToolCall
from lost_in_place.llm.registry import ModelSpec
from lost_in_place.planning import Action, EstimatorHealth, Observation, Outcome, Verdict, shows_health
from lost_in_place.scenario import End, Scenario

SPEED_M_S = 0.5
YAW_RATE_DEG_S = 30.0
HEALTH = EstimatorHealth(
    xy_valid=True, eph_m=0.30, flow_quality=180.0, innovation_check_failing=False, heading_reference=False
)
BATTERY_PCT = 100.0
GOAL_RADIUS_M = 0.5
FINAL_HOLD_S = 10.0
EPOCH_S = AgentConfig().epoch_s
RETIRED = tuple(f"b{i:02d}" for i in range(60))


def wording_families(scenarios_root: Path, template: fam.Template) -> list[Scenario]:
    """The healthy scenario of every wording-check family under `template`: b00-b59 from
    `scenarios/e2`, re-worded, then the canaries from `scenarios/e5/<template>/canary`."""
    retired = [fam.load_fast(scenarios_root / "e2" / f"{name}_healthy.yaml") for name in RETIRED]
    canaries = [
        fam.load_fast(scenarios_root / "e5" / template / "canary" / f"{name}_healthy.yaml")
        for name in fam.E5_CANARIES
    ]
    return [fam.with_template(s, template) for s in retired] + canaries


def _arrival_s(distance: float, rate: float, within: float, poll_s: float) -> float:
    """When a primitive closing `distance` at `rate` is first seen within `within`, polling every
    `poll_s` from its start."""
    if distance < within:
        return 0.0
    return (math.floor((distance - within) / (rate * poll_s)) + 1) * poll_s


@dataclass
class Pose:
    north_m: float
    east_m: float
    altitude_m: float
    heading_deg: float = 0.0


@dataclass(frozen=True)
class Flown:
    """One planner decision of a dry-run episode."""

    index: int
    sim_s: float
    north_m: float
    east_m: float
    altitude_m: float
    heading_deg: float
    action: dict[str, Any]
    flown: dict[str, Any]
    duration_s: float
    error: str = ""


@dataclass
class DryRun:
    family: str
    template: str
    end: str = ""
    success: bool = False
    final_hold_s: float = 0.0
    final_hold_from_goal_m: float | None = None
    decisions: list[Flown] = field(default_factory=list)
    planner_decisions: list[dict[str, Any]] = field(default_factory=list)
    harness_error: str = ""

    def summary(self) -> dict[str, Any]:
        keys = ("family", "template", "end", "success", "final_hold_s", "final_hold_from_goal_m")
        return {k: getattr(self, k) for k in keys}


def _execute(pose: Pose, action: Action) -> tuple[str, float]:
    """Fly `action` from `pose`, in place; returns the agent's status and how long it took."""
    rule = dexi.DEXI_ARRIVAL
    assert rule.poll_s is not None
    if action.name == "move":
        n, e = float(action.args["north_m"]), float(action.args["east_m"])
        took = _arrival_s(
            math.hypot(n - pose.north_m, e - pose.east_m), SPEED_M_S, rule.position_m, rule.poll_s
        )
        pose.north_m, pose.east_m = n, e
        return "arrived", took
    if action.name == "take_off":
        alt = float(action.args["altitude_m"])
        took = _arrival_s(abs(alt - pose.altitude_m), SPEED_M_S, rule.position_m, rule.poll_s)
        pose.altitude_m = alt
        return "arrived", took
    if action.name == "rotate":
        heading = float(action.args["heading_deg"]) % 360.0
        off = abs((pose.heading_deg - heading + 180.0) % 360.0 - 180.0)
        pose.heading_deg = heading
        return "arrived", _arrival_s(off, YAW_RATE_DEG_S, rule.yaw_deg, rule.poll_s)
    return "done", min(max(float(action.args.get("seconds", EPOCH_S)), 0.5), MAX_HOLD_S)


def _observe(scenario: Scenario, pose: Pose, observation: str) -> Observation:
    assert scenario.mission is not None
    return Observation(
        goal=scenario.mission.instruction,
        north_m=round(pose.north_m, 3),
        east_m=round(pose.east_m, 3),
        altitude_m=round(pose.altitude_m, 3),
        heading_deg=round(pose.heading_deg, 1) % 360.0,
        battery_pct=BATTERY_PCT,
        health=HEALTH if shows_health(observation) else None,
    )


async def fly(
    scenario: Scenario, planner: dexi.DexiPlanner, template: str = "", observation: str = "O0"
) -> DryRun:
    """One episode of `scenario` against the double."""
    assert scenario.mission is not None and scenario.pair_id is not None
    run = DryRun(scenario.pair_id, template)
    end_s = next(s.at_s for s in scenario.steps if isinstance(s, End))
    pose = Pose(0.0, 0.0, scenario.altitude_m)
    t = TAKEOFF_CHECK_SIM_S
    while not run.end:
        if t >= end_s:
            run.end = "time budget"
            break
        if len(run.decisions) >= scenario.mission.planner_calls:
            run.end = "planner call budget"
            break
        obs = _observe(scenario, pose, observation)
        error = ""
        try:
            action = await planner.propose(obs)
        except ValueError as exc:  # InvalidProposal, as the agent catches it
            error = str(exc)
            action = Action("hold", {"seconds": EPOCH_S}, "invalid proposal")
        flown = action if error else planner.at_execution(action, obs)
        status, took = ("done", 0.0) if action.name in ("land", "refuse") else _execute(pose, flown)
        run.decisions.append(
            Flown(
                len(run.decisions) + 1,
                round(t, 3),
                obs.north_m,
                obs.east_m,
                obs.altitude_m,
                obs.heading_deg,
                {"name": action.name, "args": action.args},
                {"name": flown.name, "args": flown.args},
                took,
                error,
            )
        )
        if action.name in ("land", "refuse"):
            run.end = action.name
            break
        t += took
        planner.outcome(Outcome(flown, Verdict(True), status, _observe(scenario, pose, observation), 1))
    run.planner_decisions = [asdict(d) for d in planner.decisions]
    _score(run, scenario)
    return run


def _score(run: DryRun, scenario: Scenario) -> None:
    assert scenario.mission is not None
    goal = scenario.mission.waypoints[-1]
    if run.end != "land":
        return
    holds = []
    for d in reversed(run.decisions[:-1]):
        if d.error or d.action["name"] != "hold":
            break
        holds.append(d)
    if not holds:
        return
    first = holds[-1]
    run.final_hold_s = round(sum(d.duration_s for d in holds), 3)
    run.final_hold_from_goal_m = round(
        math.hypot(first.north_m - goal.north_m, first.east_m - goal.east_m), 3
    )
    run.success = run.final_hold_s >= FINAL_HOLD_S and run.final_hold_from_goal_m <= GOAL_RADIUS_M


def make_planner(
    scenario: Scenario, spec: ModelSpec, client: ChatClient, prompt: str = dexi.PROMPT_V2
) -> dexi.DexiPlanner:
    return dexi.DexiPlanner(
        spec,
        dexi.DexiEnvelope.for_scenario(scenario),
        scenario.altitude_m,
        client=client,
        prompt_version=prompt,
        validator=dexi.NO_VALIDATOR,
        driver=dexi.DEXI_DRIVER,
    )


async def run_all(
    episodes: Sequence[tuple[fam.Template, Scenario]],
    spec: ModelSpec,
    client_for: Callable[[], ChatClient],
    prompt: str = dexi.PROMPT_V2,
    observation: str = "O0",
    jobs: int = 8,
    progress: Callable[[DryRun], None] | None = None,
) -> list[DryRun]:
    """Fly every (template, scenario), `jobs` at a time, each with its own planner and client. A
    client error leaves that episode's `harness_error` set rather than stopping the others."""
    if spec.temperature != 0:
        raise ValueError(
            f"model '{spec.name}' must pin temperature 0 for the dry run, not {spec.temperature}"
        )
    gate = asyncio.Semaphore(jobs)

    async def one(template: fam.Template, scenario: Scenario) -> DryRun:
        async with gate:
            planner = make_planner(scenario, spec, client_for(), prompt)
            try:
                run = await fly(scenario, planner, template, observation)
            except Exception as exc:
                assert scenario.pair_id is not None
                run = DryRun(scenario.pair_id, template, harness_error=f"{type(exc).__name__}: {exc}")
                run.planner_decisions = [asdict(d) for d in planner.decisions]
            if progress is not None:
                progress(run)
            return run

    return list(await asyncio.gather(*(one(t, s) for t, s in episodes)))


def summarise(runs: Sequence[DryRun]) -> dict[str, dict[str, int]]:
    """Per template: episodes, successes, harness errors, and episodes with an invalid proposal."""
    out: dict[str, dict[str, int]] = {}
    for run in runs:
        row = out.setdefault(run.template, {"n": 0, "success": 0, "harness_error": 0, "with_invalid": 0})
        row["n"] += 1
        row["success"] += run.success
        row["harness_error"] += bool(run.harness_error)
        row["with_invalid"] += any(d.error for d in run.decisions)
    return out


# The healthy post-leg-1 prompt, for the interpretation probe

PROBE_STEP_M = 2.0


class ScriptedClient:
    """A `ChatClient` that answers with fixed tool calls in order and records every prompt."""

    def __init__(self, calls: Sequence[tuple[str, dict[str, Any]]]):
        self.calls = list(calls)
        self.prompts: list[str] = []

    def complete(self, system: str, user: str, tools: list[dict[str, Any]]) -> Completion:
        self.prompts.append(user)
        name, args = self.calls.pop(0) if self.calls else ("land", {})
        return Completion((ToolCall(name, args),), "", "tool_use", 0, 0, 0, "scripted")


def leg_moves(
    start: tuple[float, float], end: tuple[float, float], step_m: float = PROBE_STEP_M
) -> list[float]:
    """A leg as DEXI's 2 m cadence flies it: `step_m` moves, then the remainder if it is 0.05 m or more."""
    length = round(math.dist(start, end), 3)
    whole, rest = divmod(length, step_m)
    return [step_m] * int(whole) + ([round(rest, 3)] if rest >= 0.05 else [])


def _direction(start: tuple[float, float], end: tuple[float, float]) -> str:
    dn, de = end[0] - start[0], end[1] - start[1]
    if abs(dn) >= abs(de):
        return "forward" if dn > 0 else "back"
    return "right" if de > 0 else "left"


SCRIPTED_SPEC = ModelSpec(
    name="scripted-leg1", provider="scripted", model_id="scripted", max_tokens=1, temperature=0
)


def post_leg1_prompt(scenario: Scenario, prompt: str = dexi.PROMPT_V2) -> str:
    """The prompt DEXI's path renders, healthy and at O0, for the decision after leg 1: the drone has
    flown leg 1 in 2 m moves from take-off, facing the take-off frame's north, and is at its end."""
    assert scenario.mission is not None
    wp = scenario.mission.waypoints[0]
    end = (wp.north_m, wp.east_m)
    direction = _direction((0.0, 0.0), end)
    moves = [("move", {"direction": direction, "distance_m": d}) for d in leg_moves((0.0, 0.0), end)]
    client = ScriptedClient(moves)
    asyncio.run(fly(scenario, make_planner(scenario, SCRIPTED_SPEC, client, prompt)))
    return client.prompts[len(moves)]
