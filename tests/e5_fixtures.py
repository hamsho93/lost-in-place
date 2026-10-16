"""Synthetic recorded episodes for the Phase 0, probe and admission tests: E4-shaped (`dexi`, prompt
9b2e7a7, O1) and wave-shaped, rendered through the adapter, written as a local runs mirror."""

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from lost_in_place.llm import dexi
from lost_in_place.planning import EstimatorHealth, shows_health

ROOT = Path(__file__).resolve().parents[1]
MISSION = "Take off, fly 4.0 m left, then a further 1.8 m forward, hold there for 10 s, then land."
ENVELOPE = dexi.DexiEnvelope(max_alt_m=2.0, geofence_m=4.5)
OK = {"xy_valid": True, "eph_m": 0.31, "flow_quality": 176.0, "innovation_check_failing": False}
OK |= {"heading_reference": False}
NO_FLOW = {**OK, "eph_m": 1.2, "flow_quality": 0.0}
LATE = {**OK, "flow_quality": 140.0}


def load_script(name: str) -> ModuleType:
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _health(h: dict[str, Any]) -> EstimatorHealth:
    return EstimatorHealth(**h)


def render(
    state: dict[str, Any], health: dict[str, Any], history: list[str], observation: str, prompt: str
) -> str:
    block = dexi.health_block(_health(health), observation) if shows_health(observation) else ""
    return dexi.render_prompt(
        MISSION, ENVELOPE, state, history, block, dexi.template(prompt, dexi.NO_VALIDATOR, observation)
    )


def episode(
    family: str,
    state: str,
    observation: str = "O1",
    prompt: str = dexi.PROMPT_VERSION,
    model: str = "claude-sonnet-4-6",
    informed: bool = True,
    rejected: bool = False,
    outcome: str = "completed",
) -> dict[str, Any]:
    """Four planner decisions: two before the onset at 25 s, one just after it with flow still good,
    and (if `informed`) one showing the fault, at +5.5 s in loud and +2.2 s in brief."""
    onset = 25.0
    informed_s = onset + (5.5 if state == "loud" else 2.2)
    steps = [(20.0, OK, (0.01, 0.01)), (23.0, OK, (0.0, -2.0)), (25.4, LATE, (0.0, -2.6))]
    if informed:
        steps.append((informed_s, NO_FLOW, (0.0, -3.2)))
    history = [dexi.history_line(1, "take_off", {"alt_m": 1.5}, "reached: took off, now at 1.31 m")]
    decisions, prompts = [], []
    for i, (sim_s, health, (north, east)) in enumerate(steps, start=1):
        st = {
            "north_m": north,
            "east_m": east,
            "rel_alt_m": 1.5,
            "yaw_deg": 0.0,
            "armed": True,
            "in_air": True,
        }
        st |= {"flight_mode": "OFFBOARD", "battery_pct": 88.0 - i}
        prompts.append(
            {"user_prompt": render(st, health, history, observation, prompt), "input_tokens": 1400}
        )
        decisions.append({"index": i, "sim_us": int(sim_s * 1e6), "continued": False, "health": health})
        decisions.append({"index": i, "sim_us": int((sim_s + 1) * 1e6), "continued": True, "health": health})
        if rejected and i == 1:
            history.append(
                dexi.history_line(
                    i + 1, "take_off", {"alt_m": 1.5}, "REJECTED by validator: already in the air"
                )
            )
        else:
            args = {"direction": "left", "distance_m": 2.0}
            history.append(
                dexi.history_line(
                    i + 1, "move", args, f"reached: reached ({north:.1f} N, {east:.1f} E, 1.5 m alt)"
                )
            )
    return {
        "scenario_id": f"{family}_{state}",
        "outcome": outcome,
        "scenario": {"id": f"{family}_{state}", "state": "degraded_safe" if state == "brief" else state},
        "agent": {
            "planner": "dexi",
            "planner_options": {"model": model, "prompt": prompt},
            "observation": observation,
        },
        "events": [{"sim_s": onset, "action": "inject", "unit": "optical_flow", "type": "off"}],
        "decisions": decisions,
        "planner_decisions": prompts,
    }


def write(
    root: Path,
    run: str,
    key: str,
    label: str,
    ep: dict[str, Any],
    mtime: float,
    score: dict[str, Any] | None = None,
) -> None:
    folder = root / run / key / label
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "episode.json").write_text(json.dumps(ep))
    os.utime(folder / "episode.json", (mtime, mtime))
    if score is not None:
        (folder / "score.json").write_text(json.dumps(score))


def e4_mirror(root: Path, families: int = 4, uninformed: tuple[str, ...] = ()) -> Path:
    """An E4-shaped run: families b00.. at O1 and O0, loud, brief and healthy, two reps; rep 1 of b00
    loud has a harness-failed attempt before its counted one."""
    for f in range(families):
        family = f"b{f:02d}"
        for state in ("loud", "brief", "healthy"):
            for rep in (0, 1):
                for obs in ("O0", "O1"):
                    key = f"{family}_{state}/dexi-claude-sonnet-4-6-none-{obs}/r{rep:03d}-s{rep + 7}"
                    informed = f"{family}_{state}" not in uninformed
                    ep = episode(family, state, observation=obs, informed=informed, rejected=f == 1)
                    write(root, "e4-claude", key, "b-attempt2", ep, 2000.0)
                    if f == 0 and rep == 1 and state == "loud":
                        failed = episode(family, state, observation=obs, outcome="boot_stall")
                        failed["planner_decisions"] = []
                        write(root, "e4-claude", key, "a-attempt1", failed, 1000.0)
    return root
