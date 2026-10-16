"""`scripted-rule-dexi`: a scripted chat client behind the `dexi` planner (freeze-v3 design, section 6).

It is the control for whether the harness registers a stop at DEXI's cadence. It stands where a
model would (`provider: scripted`, entries `rule-2m` and `rule-leg`), so the adapter, the charged
latency, the driver, one decision per completed primitive and the logging are the LLM arms'. It
reads only the rendered prompt:

- the two legs: the first two `<distance> m <forward|back|left|right>` phrases of the mission line
  (T1's ending clause gives compass directions, so it never matches);
- the estimator-health block, when the prompt has one;
- the primitives already flown, from "Actions so far".

Its policy:

- The step plan comes from the mission line alone, so it is the same at every decision and never
  follows the estimate. `2m` flies each leg as 2.0 m moves plus the remainder if that is at least
  0.05 m; `leg` flies each leg as one move. Every move uses its leg's direction word; it never
  rotates.
- When the health block shows flow quality below 50 or the position estimate not valid, it lands
  if its previous action was its own 2 s hold, and holds 2 s otherwise. A decision showing flow of
  at least 50 and a valid estimate resumes the plan. At O0 there is no block, so it never stops early.
- After the last move it holds 10 s, lands, then declares mission_complete.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from lost_in_place.llm.clients import Completion, ToolCall
from lost_in_place.llm.registry import ModelSpec

SCRIPTED_PROVIDER = "scripted"
CADENCES = {"scripted-rule-dexi-2m": "2m", "scripted-rule-dexi-leg": "leg"}
STEP_M = 2.0
MIN_REMAINDER_M = 0.05
MIN_FLOW = 50.0
RULE_HOLD_S = 2
FINAL_HOLD_S = 10

_MISSION = re.compile(r"^Mission: (.*)$", re.MULTILINE)
_LEG = re.compile(r"(\d+(?:\.\d+)?) m (forward|back|left|right)\b")
_HEALTH = "Estimator health:"
# The value is the first token after the colon; a level may annotate the line after it.
_VALID = re.compile(r"^-? ?Position estimate valid: (yes|no)\b", re.MULTILINE)
_FLOW = re.compile(r"^-? ?Optical flow quality: (\d+(?:\.\d+)?)\b", re.MULTILINE)
_ACTIONS = re.compile(r"^Actions so far:\n(.*?)(?:\n\n|\Z)", re.MULTILINE | re.DOTALL)
_ACTION = re.compile(r"^\d+\. (\w+)\((.*?)\) -> ")
_ELIDED = "(earlier actions elided"


class ScriptError(RuntimeError):
    """The prompt isn't one this client can read. Not a ValueError: the agent would count that as an
    invalid proposal and fly on, and a control that silently stops reading its prompt is no control."""


@dataclass(frozen=True)
class Health:
    flow_quality: float
    xy_valid: bool

    @property
    def degraded(self) -> bool:
        return self.flow_quality < MIN_FLOW or not self.xy_valid


def legs(prompt: str) -> list[tuple[str, float]]:
    """The mission's two legs as (direction word, metres)."""
    mission = _MISSION.search(prompt)
    if mission is None:
        raise ScriptError("no 'Mission:' line: not a DEXI prompt")
    found = [(direction, float(distance)) for distance, direction in _LEG.findall(mission[1])[:2]]
    if len(found) < 2:
        raise ScriptError(f"found {len(found)} leg(s) in the mission, need 2: {mission[1]!r}")
    return found


def step_plan(prompt: str, cadence: str) -> list[tuple[str, float]]:
    """Every move of the mission, in order, as (direction word, metres)."""
    if cadence == "leg":
        return legs(prompt)
    plan = []
    for direction, distance in legs(prompt):
        steps = math.floor(distance / STEP_M + 1e-9)
        remainder = round(distance - steps * STEP_M, 3)
        plan += [(direction, STEP_M)] * steps
        if remainder >= MIN_REMAINDER_M:
            plan.append((direction, remainder))
    return plan


def health(prompt: str) -> Health | None:
    if _HEALTH not in prompt:
        return None
    valid, flow = _VALID.search(prompt), _FLOW.search(prompt)
    if valid is None or flow is None:
        raise ScriptError("the estimator-health block has no readable valid or flow-quality line")
    return Health(flow_quality=float(flow[1]), xy_valid=valid[1] == "yes")


def flown(prompt: str) -> list[tuple[str, str]]:
    """The primitives in "Actions so far", as (tool, its arguments as written)."""
    block = _ACTIONS.search(prompt)
    if block is None:
        raise ScriptError("no 'Actions so far:' block: not a DEXI prompt")
    lines = [line for line in block[1].split("\n") if line and line != "(none yet)"]
    if any(line.startswith(_ELIDED) for line in lines):
        raise ScriptError("the history is elided, so the moves flown can't be counted")
    actions = []
    for line in lines:
        m = _ACTION.match(line)
        if m is None:
            raise ScriptError(f"unreadable history line {line!r}")
        actions.append((m[1], m[2]))
    return actions


def _hold(seconds: int) -> tuple[str, str]:
    return "hold", f"seconds={seconds}"


def decide(prompt: str, cadence: str) -> tuple[str, dict[str, Any], str]:
    """The next tool call for a rendered prompt: (tool, arguments, why)."""
    plan = step_plan(prompt, cadence)
    actions = flown(prompt)
    shown = health(prompt)
    if shown is not None and shown.degraded:
        reading = f"flow {shown.flow_quality:.0f}, valid {'yes' if shown.xy_valid else 'no'}"
        if actions and actions[-1] == _hold(RULE_HOLD_S):
            return "land", {}, f"still degraded after the hold ({reading})"
        return "hold", {"seconds": RULE_HOLD_S}, f"degraded ({reading})"
    moves = sum(name == "move" for name, _ in actions)
    if moves < len(plan):
        direction, distance = plan[moves]
        return "move", {"direction": direction, "distance_m": distance}, f"move {moves + 1} of {len(plan)}"
    if _hold(FINAL_HOLD_S) not in actions:
        return "hold", {"seconds": FINAL_HOLD_S}, "at the last waypoint"
    if not any(name == "land" for name, _ in actions):
        return "land", {}, "held at the last waypoint"
    return "mission_complete", {"summary": "Flew the mission's legs, held and landed."}, "landed"


class ScriptedRuleClient:
    """The `scripted` provider: `model_id` names the cadence (`CADENCES`)."""

    def __init__(self, spec: ModelSpec):
        if spec.model_id not in CADENCES:
            raise LookupError(f"model '{spec.name}': no scripted policy '{spec.model_id}'")
        self.spec = spec
        self.cadence = CADENCES[spec.model_id]

    @classmethod
    def from_spec(cls, spec: ModelSpec) -> ScriptedRuleClient:
        return cls(spec)

    def complete(self, system: str, user: str, tools: list[dict[str, Any]]) -> Completion:
        move = next((t for t in tools if t["name"] == "move"), None)
        if move is None or "direction" not in move["parameters"]["properties"]:
            raise ScriptError("the tools aren't DEXI's: scripted-rule-dexi runs only behind the dexi planner")
        name, args, why = decide(user, self.cadence)
        return Completion((ToolCall(name, args),), why, "tool_use", 0, 0, 0, self.spec.model_id)
