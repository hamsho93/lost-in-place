"""The DEXI-prompt planner. Fixtures in `fixtures/dexi_9b2e7a7/` were recorded by DEXI's own
`PlannerLoop` at hamsho93/drones 9b2e7a7 (a scripted client on its `SimulatedDriver`)."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from lost_in_place.llm.clients import Completion, ToolCall
from lost_in_place.llm.dexi import (
    TOOLS,
    DexiEnvelope,
    DexiPlanner,
    describe_state,
    history_line,
    render_prompt,
    state_from_observation,
)
from lost_in_place.llm.prompt import InvalidProposal
from lost_in_place.llm.registry import get_model
from lost_in_place.manifest import resolve_arm
from lost_in_place.planning import (
    Action,
    EstimatorHealth,
    Observation,
    Outcome,
    PrimitivePlanner,
    Verdict,
    arm_name,
)
from lost_in_place.scenario import load_scenario

FIXTURES = Path(__file__).parent / "fixtures" / "dexi_9b2e7a7"
E2 = Path(__file__).parents[1] / "scenarios" / "e2"


def test_tools_are_dexis_then_refuse() -> None:
    dexi = json.loads((FIXTURES / "tools.json").read_text())
    # Key order too: the chat template serialises the tools into the prompt.
    assert json.dumps(TOOLS[:-1]) == json.dumps(dexi)
    assert TOOLS[-1]["name"] == "refuse" and TOOLS[-1]["parameters"]["required"] == ["reason"]


def test_prompts_match_the_ones_dexis_loop_wrote() -> None:
    cfg = json.loads((FIXTURES / "envelope.json").read_text())
    envelope = DexiEnvelope(cfg["max_alt_m"], cfg["geofence_m"], cfg["max_move_m"], cfg["max_hold_s"])
    records = [json.loads(line) for line in (FIXTURES / "loop.jsonl").read_text().splitlines()]
    history: list[str] = []
    for record in records:
        assert render_prompt(cfg["mission"], envelope, record["state"], history) == record["prompt"]
        if record["valid"]:
            result = f"{record['outcome']['status']}: {record['outcome']['detail']}"
        else:
            result = f"REJECTED by validator: {record['reject_reason']}"
        history.append(history_line(record["step"], record["tool"], record["tool_input"], result))
    assert len(records) > 13  # far enough for the history window to elide


def test_the_state_line_is_rounded_and_wrapped_like_dexis_driver() -> None:
    obs = Observation("g", -0.004, east_m=1.234, altitude_m=1.499, heading_deg=359.96, battery_pct=87.4)
    assert describe_state(state_from_observation(obs)) == (
        "north 0.0 m, east 1.23 m, altitude 1.5 m, heading 0.0 deg, armed, in air, battery 87%, mode OFFBOARD"
    )
    turned = Observation("g", 0.0, 0.0, 1.5, heading_deg=190.04, battery_pct=90)
    assert state_from_observation(turned)["yaw_deg"] == -170.0


def test_the_envelope_box_holds_every_waypoint_half_a_metre_in() -> None:
    scenario = load_scenario(E2 / "b00_healthy.yaml")  # farthest waypoint 5.4 m west
    assert DexiEnvelope.for_scenario(scenario) == DexiEnvelope(max_alt_m=2.0, geofence_m=6.0)
    fault = load_scenario(E2 / "b00_loud.yaml")
    assert DexiEnvelope.for_scenario(fault) == DexiEnvelope.for_scenario(scenario)


class Client:
    def __init__(self, *calls: tuple[str, dict[str, Any]] | None) -> None:
        self.calls = list(calls)
        self.requests: list[tuple[str, str, list[dict[str, Any]]]] = []

    def complete(self, system: str, user: str, tools: list[dict[str, Any]]) -> Completion:
        self.requests.append((system, user, tools))
        call = self.calls.pop(0)
        return Completion(
            tool_calls=() if call is None else (ToolCall(*call),),
            text="",
            stop_reason="stop",
            input_tokens=900,
            output_tokens=25,
            latency_ms=300,
            model_id="m@abc",
        )


def planner(*calls: tuple[str, dict[str, Any]] | None) -> DexiPlanner:
    envelope = DexiEnvelope(max_alt_m=2.0, geofence_m=6.0)
    return DexiPlanner(get_model("qwen3-4b-student"), envelope, 1.5, client=Client(*calls))


def at(north: float = 1.0, east: float = 1.0, heading: float = 90.0, altitude: float = 1.49) -> Observation:
    return Observation("Fly to the door, then land.", north, east, altitude, heading, 92.0)


def propose(p: DexiPlanner, obs: Observation) -> Action:
    return asyncio.run(p.propose(obs))


def done(p: DexiPlanner, action: Action, status: str = "arrived", obs: Observation | None = None) -> None:
    p.outcome(Outcome(action, Verdict(True), status, obs or at(), epochs=1))


def test_sends_one_user_message_with_dexis_tools_and_seeds_the_take_off() -> None:
    p = planner(("hold", {"seconds": 2}))
    assert isinstance(p, PrimitivePlanner)
    propose(p, at())
    system, user, tools = p.client.requests[0]  # type: ignore[attr-defined]
    assert system == "" and tools is TOOLS
    assert "Safety envelope: altitude cap 2.0 m; stay within +/-6.0 m of home" in user
    assert "single move at most 2.0 m; hold at most 30 s." in user
    assert "Current state: north 1.0 m, east 1.0 m, altitude 1.49 m, heading 90.0 deg, armed" in user
    assert "Actions so far:\n1. take_off(alt_m=1.5) -> reached: took off, now at 1.49 m\n" in user
    (decision,) = p.decisions
    assert decision.prompt_version == "dexi-9b2e7a7" and decision.user_prompt == user


def test_relative_moves_and_turns_become_absolute_targets() -> None:
    p = planner(
        ("move", {"direction": "forward", "distance_m": 2}),
        ("move", {"direction": "left", "distance_m": 1.0}),
        ("rotate", {"deg": -100}),
        ("move", {"direction": "up", "distance_m": 0.5}),
    )
    forward = propose(p, at(heading=90.0))
    assert forward == Action("move", {"north_m": 1.0, "east_m": 3.0})
    done(p, forward)
    left = propose(p, at(heading=90.0))  # facing east, left is north
    assert left == Action("move", {"north_m": 2.0, "east_m": 1.0})
    done(p, left)
    turn = propose(p, at(heading=10.0))
    assert turn == Action("rotate", {"heading_deg": 270.0})
    done(p, turn, obs=at(heading=271.0))
    climb = propose(p, at())
    assert climb == Action("take_off", {"altitude_m": 1.99})
    done(p, climb)
    assert p.history[1:] == [
        "2. move(direction=forward, distance_m=2) -> reached: reached (1.0 N, 3.0 E, 1.5 m alt)",
        "3. move(direction=left, distance_m=1.0) -> reached: reached (2.0 N, 1.0 E, 1.5 m alt)",
        "4. rotate(deg=-100) -> reached: rotated to heading -89 deg",
        "5. move(direction=up, distance_m=0.5) -> reached: reached (1.0 N, 1.0 E, 2.0 m alt)",
    ]


def test_what_cant_be_flown_is_rejected_in_dexis_words() -> None:
    p = planner(
        ("take_off", {"alt_m": 1.0}),
        ("mission_complete", {"summary": "done"}),
        None,
        ("fly_to", {"north_m": 1}),
        ("move", {"direction": "sideways", "distance_m": 1}),
        ("move", {"direction": "down", "distance_m": 1.4}),
    )
    for _ in range(6):
        with pytest.raises(InvalidProposal):
            propose(p, at())
        p.outcome(Outcome(Action("hold", {"seconds": 1.5}), Verdict(True), "done", at(), epochs=1))
    assert p.history[1:] == [
        "2. take_off(alt_m=1.0) -> REJECTED by validator: already in the air",
        "3. mission_complete(summary=done) -> REJECTED by validator: cannot complete mission while airborne"
        " or armed — land first",
        "4. None() -> REJECTED by validator: no tool call (stop_reason=stop)",
        "5. fly_to(north_m=1) -> REJECTED by validator: tool 'fly_to' not in goal allowlist",
        "6. move(direction=sideways, distance_m=1) -> REJECTED by validator: direction must be one of"
        " ('forward', 'back', 'left', 'right', 'up', 'down')",
        "7. move(direction=down, distance_m=1.4) -> REJECTED by validator: move would end at 0.09 m — use"
        " land instead",
    ]
    assert p.decisions[0].error == "already in the air"


def test_outcomes_read_like_dexis_driver() -> None:
    p = planner(
        ("move", {"direction": "forward", "distance_m": 2.0}),
        ("move", {"direction": "forward", "distance_m": 2.0}),
        ("move", {"direction": "forward", "distance_m": 2.0}),
        ("hold", {"seconds": 10}),
        ("refuse", {"reason": "The position estimate is invalid."}),
    )
    move = propose(p, at(heading=0.0))
    assert p.timeout_s(move) == 14.0  # DEXI's 10 s plus 0.5 m/s
    p.outcome(Outcome(move, Verdict(False, "enters keep-out 'table'"), "done", at(), epochs=0))
    move = propose(p, at(heading=0.0))
    p.outcome(Outcome(move, Verdict(False, "eph 1.30 m"), "done", at(), epochs=2))
    move = propose(p, at(heading=0.0))
    under_way = "still under way after 1.5 s, 1.2 m from the target"
    p.outcome(Outcome(move, Verdict(True), under_way, at(), epochs=9, timed_out=True))
    hold = propose(p, at())
    done(p, hold, "done")
    assert p.history[1:] == [
        "2. move(direction=forward, distance_m=2.0) -> REJECTED by validator: enters keep-out 'table'",
        "3. move(direction=forward, distance_m=2.0) -> failed: stopped by the validator: eph 1.30 m",
        "4. move(direction=forward, distance_m=2.0) -> timeout: did not reach target (3.0 N, 1.0 E, 1.5 m)"
        " within 14s",
        "5. hold(seconds=10) -> reached: held for 10 s",
    ]
    assert propose(p, at()) == Action("refuse", reason="The position estimate is invalid.")


def test_o1_puts_the_health_block_under_the_state_line() -> None:
    p = planner(("land", {}))
    health = EstimatorHealth(
        xy_valid=False, eph_m=1.3, flow_quality=12.0, innovation_check_failing=True, heading_reference=False
    )
    obs = Observation("Fly to the door, then land.", 1.0, 1.0, 1.49, 90.0, 92.0, health=health)
    assert propose(p, obs) == Action("land")
    user = p.client.requests[0][1]  # type: ignore[attr-defined]
    assert "mode OFFBOARD\nEstimator health:\n- Position estimate valid: no\n" in user
    assert "- Heading reference: none\n\nActions so far:" in user


def test_a_sweep_names_a_dexi_arm_by_planner_and_model() -> None:
    planner_name, options = resolve_arm("dexi/qwen3-4b-student", {})
    assert (planner_name, options) == ("dexi", {"model": "qwen3-4b-student"})
    assert arm_name(planner_name, options) == "dexi-qwen3-4b-student"
    assert resolve_arm("qwen3-4b-student", {}) == ("llm", {"model": "qwen3-4b-student"})
    with pytest.raises(LookupError):
        resolve_arm("nope/qwen3-4b-student", {})
