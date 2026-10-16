"""Freeze-v3's render grid: the prompts an E5 arm can be shown, rendered by the planner itself, and
hashed into the v3 prompt bundle (`freeze.prompt_bundle_v3`), so a change anywhere on DEXI's
rendering path changes the bundle's hash, not only a change at the bundle's exemplars.

A scripted client drives `DexiPlanner` through fixed histories under the `dexi` driver, with each
move re-anchored at execution: moves that arrive, time out, are interrupted or are stopped,
invalid calls of every kind, and a history longer than DEXI's window. For every observation level
and prompt version the adapter accepts under validator `none`, the grid renders:
- a sweep of estimator health on the nominal history: flow quality and position uncertainty at and
  around the rungs' thresholds and the health lines' rounding, crossed with every flag;
- every history, at the bundle's healthy, brief and loud health (`freeze.v3_observations`, the
  golden renders' values), in both templates of the first canary;
- the nominal history in the T0 and T1 wording of every tenth E5 family and every canary, at those
  three healths, each with its own envelope.

Every `WIRE_EVERY`-th request is also rendered as each Bedrock model in the registry would send it:
the Converse request body, or the imported model's chat template. The summary keeps one digest per
(level, version) pair and per model, so a mismatch says where.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

from lost_in_place.llm.clients import BedrockConverseClient, BedrockImportedClient, Completion, ToolCall
from lost_in_place.llm.dexi import (
    DEXI_DRIVER,
    NO_VALIDATOR,
    PROMPT_VERSIONS,
    DexiEnvelope,
    DexiPlanner,
    template,
)
from lost_in_place.llm.prompt import InvalidProposal
from lost_in_place.llm.registry import ModelSpec, load_models
from lost_in_place.planning import (
    OBSERVATION_LEVELS,
    Action,
    EstimatorHealth,
    Observation,
    Outcome,
    Verdict,
    shows_health,
)
from lost_in_place.scenario import Scenario, load_scenario

FLOWS = (0.0, 49.0, 49.5, 50.0, 159.0, 160.0, 255.0)
EPHS = (0.3, 0.69, 0.695, 0.7, 0.705, 0.71, 1.5)
FAMILY_EVERY = 10
WIRE_EVERY = 25
BATCH = 256
EXECUTION_DRIFT_M = (0.013, -0.021)


@dataclasses.dataclass(frozen=True)
class Step:
    """One planner reply and what became of it: an `Outcome` status, or "" for an invalid call."""

    tool: str | None
    args: dict[str, Any]
    result: str


SCRIPTS: dict[str, tuple[Step, ...]] = {
    "nominal": (
        Step("move", {"direction": "forward", "distance_m": 2.0}, "arrived"),
        Step("move", {"direction": "right", "distance_m": 1.4}, "arrived"),
        Step("rotate", {"deg": 90}, "arrived"),
        Step("move", {"direction": "up", "distance_m": 0.3}, "arrived"),
        Step("hold", {"seconds": 2}, "done"),
        Step("hold", {"seconds": 10}, "done"),
        Step("land", {}, "done"),
    ),
    "faults": (
        Step(None, {}, ""),
        Step("move", {"direction": "sideways", "distance_m": 1.0}, ""),
        Step("hold", {"seconds": 0}, ""),
        Step("take_off", {"alt_m": 1.5}, ""),
        Step("mission_complete", {}, ""),
        Step("move", {"direction": "down", "distance_m": 5.0}, ""),
        Step("fly", {}, ""),
        Step("refuse", {"reason": " "}, ""),
        Step("move", {"direction": "back", "distance_m": 1.0}, "timeout"),
        Step("rotate", {"deg": -45}, "timeout"),
        Step("move", {"direction": "left", "distance_m": 2.0}, "interrupted"),
        Step("move", {"direction": "forward", "distance_m": 0.5}, "rejected"),
        Step("move", {"direction": "forward", "distance_m": 0.5}, "stopped"),
        Step("refuse", {"reason": "the position estimate looks wrong"}, "done"),
    ),
    "long": tuple(
        Step("move", {"direction": d, "distance_m": 0.5}, "arrived") for d in ("forward", "back") * 7
    ),
}


class _Scripted:
    """A client that replies with the current step's call and keeps every request it was sent."""

    def __init__(self) -> None:
        self.step = Step(None, {}, "")
        self.requests: list[tuple[str, str, list[dict[str, Any]]]] = []

    def complete(self, system: str, user: str, tools: list[dict[str, Any]]) -> Completion:
        self.requests.append((system, user, tools))
        calls = () if self.step.tool is None else (ToolCall(self.step.tool, dict(self.step.args)),)
        return Completion(calls, "", "tool_use", 0, 0, 0, "render-grid")


def pairs() -> list[tuple[str, str]]:
    """Every (observation level, prompt version) the adapter accepts under validator `none`."""
    out = []
    for version in PROMPT_VERSIONS:
        for level in OBSERVATION_LEVELS:
            try:
                template(version, NO_VALIDATOR, level)
            except ValueError:
                continue
            out.append((level, version))
    return out


def health_sweep() -> list[EstimatorHealth]:
    return [
        EstimatorHealth(valid, eph, flow, innovation_check_failing=innovation, heading_reference=heading)
        for flow in FLOWS
        for eph in EPHS
        for valid in (True, False)
        for innovation in (False, True)
        for heading in (False, True)
    ]


def _outcome(action: Action, step: Step, obs: Observation) -> Outcome:
    north, east, alt, heading = obs.north_m, obs.east_m, obs.altitude_m, obs.heading_deg
    if step.result == "arrived":
        if action.name == "move":
            north, east = action.args["north_m"], action.args["east_m"]
        elif action.name == "take_off":
            alt = action.args["altitude_m"]
        elif action.name == "rotate":
            heading = action.args["heading_deg"]
    elif step.result == "timeout":
        north, east = north + 0.4, east - 0.2
    after = dataclasses.replace(obs, north_m=north, east_m=east, altitude_m=alt, heading_deg=heading)
    verdict = (
        Verdict(False, "outside the geofence") if step.result in ("rejected", "stopped") else Verdict(True)
    )
    status = {
        "timeout": "flying",
        "interrupted": "interrupted: PX4 failsafe (land)",
        "rejected": "rejected",
        "stopped": "rejected",
    }.get(step.result, step.result)
    epochs = {"rejected": 0, "stopped": 2}.get(step.result, 3)
    return Outcome(action, verdict, status, after, epochs, timed_out=step.result == "timeout")


async def _fly(
    scenario: Scenario, level: str, version: str, script: tuple[Step, ...], health: EstimatorHealth | None
) -> list[tuple[str, str, list[dict[str, Any]]]]:
    assert scenario.mission is not None
    client = _Scripted()
    spec = ModelSpec(name="render-grid", provider="render-grid", model_id="render-grid", max_tokens=512)
    planner = DexiPlanner(
        spec,
        DexiEnvelope.for_scenario(scenario),
        scenario.altitude_m,
        client=client,
        prompt_version=version,
        validator=NO_VALIDATOR,
        driver=DEXI_DRIVER,
        observation=level,
    )
    goal = scenario.mission.instruction
    obs = Observation(goal, 0.0, 0.0, scenario.altitude_m, 0.0, 92.0, health=health)
    for i, step in enumerate(script):
        obs = dataclasses.replace(obs, battery_pct=92.0 - 1.5 * i)
        client.step = step
        try:
            proposed = await planner.propose(obs)
        except InvalidProposal:
            continue
        drift = dataclasses.replace(
            obs, north_m=obs.north_m + EXECUTION_DRIFT_M[0], east_m=obs.east_m + EXECUTION_DRIFT_M[1]
        )
        flown = planner.at_execution(proposed, drift)
        outcome = _outcome(flown, step, drift)
        planner.outcome(outcome)
        obs = outcome.observation
    return client.requests


def _scenarios(root: Path, template_dir: str, every: int) -> list[Path]:
    families = sorted((root / "scenarios" / "e5" / template_dir / "families").glob("c*_healthy.yaml"))
    canaries = sorted((root / "scenarios" / "e5" / template_dir / "canary").glob("k*_healthy.yaml"))
    if not families or not canaries:
        raise FileNotFoundError(
            f"no E5 families or canaries under {root / 'scenarios' / 'e5' / template_dir}"
        )
    return [*families[::every], *canaries]


def _cells(root: Path) -> list[tuple[str, str, str, EstimatorHealth | None, tuple[Step, ...]]]:
    from lost_in_place.freeze import v3_observations

    exemplars = list(v3_observations().values())
    sample = {t: _scenarios(root, t, FAMILY_EVERY) for t in ("t0", "t1")}
    first_canary = {t: next(p for p in paths if p.name.startswith("k")) for t, paths in sample.items()}
    cells = []
    for level, version in pairs():
        sweep: list[EstimatorHealth | None] = [*health_sweep()] if shows_health(level) else [None]
        at: list[EstimatorHealth | None] = [*exemplars] if shows_health(level) else [None]
        for health in sweep:
            cells.append((level, version, str(first_canary["t1"]), health, SCRIPTS["nominal"]))
        for t in ("t0", "t1"):
            for script in SCRIPTS.values():
                for health in at:
                    cells.append((level, version, str(first_canary[t]), health, script))
            for path in sample[t]:
                for health in at:
                    cells.append((level, version, str(path), health, SCRIPTS["nominal"]))
    return cells


def _wire_clients() -> dict[str, Any]:
    clients: dict[str, Any] = {}
    for name, spec in sorted(load_models().items()):
        if spec.provider == "bedrock":
            clients[name] = BedrockConverseClient(spec, client=object(), env={})
        elif spec.provider == "bedrock_imported":
            clients[name] = BedrockImportedClient(spec, runtime=object(), control=object())
    return clients


def _wire(client: Any, system: str, user: str, tools: list[dict[str, Any]]) -> bytes:
    if isinstance(client, BedrockImportedClient):
        return client.render(system, user, tools).encode()
    return json.dumps(client.request(system, user, tools), sort_keys=True, separators=(",", ":")).encode()


def render_grid(root: Path) -> dict[str, Any]:
    """The grid's digests: overall, by (level, version) pair and by registry model, with its size."""
    root = root.resolve()
    loaded: dict[str, Scenario] = {}
    by_pair = {f"{level}/{version}": hashlib.sha256() for level, version in pairs()}
    total = hashlib.sha256()
    clients = _wire_clients()
    wire = {name: hashlib.sha256() for name in clients}
    cells = _cells(root)
    count = 0

    for _, _, path, _, _ in cells:
        if path not in loaded:
            loaded[path] = load_scenario(path)

    async def run() -> list[list[tuple[str, str, list[dict[str, Any]]]]]:
        out: list[list[tuple[str, str, list[dict[str, Any]]]]] = []
        for i in range(0, len(cells), BATCH):
            batch = cells[i : i + BATCH]
            out += await asyncio.gather(*(_fly(loaded[p], lv, v, s, h) for lv, v, p, h, s in batch))
        return out

    tools_seen: dict[int, bytes] = {}
    for (level, version, *_), requests in zip(cells, asyncio.run(run()), strict=True):
        for system, user, tools in requests:
            tools_bytes = tools_seen.setdefault(id(tools), json.dumps(tools, sort_keys=True).encode())
            record = b"\0".join((system.encode(), user.encode(), tools_bytes)) + b"\x1e"
            by_pair[f"{level}/{version}"].update(record)
            total.update(record)
            if count % WIRE_EVERY == 0:
                for name, client in clients.items():
                    wire[name].update(_wire(client, system, user, tools) + b"\x1e")
            count += 1
    return {
        "prompts": count,
        "sha256": total.hexdigest(),
        "by_pair": {pair: h.hexdigest() for pair, h in by_pair.items()},
        "wire_every": WIRE_EVERY,
        "wire": {name: h.hexdigest() for name, h in wire.items()},
        "scenarios": sorted(Path(p).relative_to(root).as_posix() for p in loaded),
    }
