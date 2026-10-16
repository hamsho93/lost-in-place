"""The DEXI-prompt planner. Fixtures in `fixtures/dexi_9b2e7a7/` were recorded by DEXI's own
`PlannerLoop` at hamsho93/drones 9b2e7a7 (a scripted client on its `SimulatedDriver`)."""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from lost_in_place.llm.clients import Completion, ToolCall
from lost_in_place.llm.dexi import (
    DEXI_ARRIVAL,
    PROMPT,
    PROMPT_V2,
    PROMPT_VERSION,
    TOOLS,
    VALIDATOR_CLAIM,
    DexiEnvelope,
    DexiPlanner,
    check_options,
    describe_state,
    history_line,
    make_planner,
    render_prompt,
    state_from_observation,
    template,
)
from lost_in_place.llm.prompt import InvalidProposal
from lost_in_place.llm.registry import get_model
from lost_in_place.manifest import resolve_arm
from lost_in_place.planning import (
    Action,
    Arrival,
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
    scenario = load_scenario(E2 / "b00_healthy.yaml")  # farthest waypoint 5.0 m east
    assert DexiEnvelope.for_scenario(scenario) == DexiEnvelope(max_alt_m=2.0, geofence_m=5.5)
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


def test_the_frozen_prompt_is_dexis_whatever_the_validator() -> None:
    assert template(PROMPT_VERSION, "none") == template(PROMPT_VERSION, "geometric") == PROMPT
    assert PROMPT_VERSION == "dexi-9b2e7a7"
    assert hashlib.sha256(PROMPT.encode()).hexdigest() == (
        "56fc6e789e8b3f3fd7194a6fe7cda70d73f3bef37e201e2584c0a2f8f1ba7c46"
    )


def test_dexi_v2_claims_a_validator_only_when_there_is_one() -> None:
    assert template(PROMPT_V2, "geometric") == template(PROMPT_V2, "estimator_health") == PROMPT
    bare = template(PROMPT_V2, "none")
    assert "validator" not in bare
    assert bare.startswith(
        "You are the mission planner for a small indoor quadcopter. Each turn you\n"
        "choose exactly one action from the provided tools.\n\nMission: {mission}\n"
    )
    assert bare == PROMPT.replace(VALIDATOR_CLAIM, "")
    with pytest.raises(ValueError, match="no DEXI prompt version 'dexi-v3'"):
        template("dexi-v3", "none")


def v2(
    *calls: tuple[str, dict[str, Any]] | None, validator: str = "none", driver: str = "freeze-v2"
) -> DexiPlanner:
    envelope = DexiEnvelope(max_alt_m=2.0, geofence_m=6.0)
    return DexiPlanner(
        get_model("claude-sonnet-4-6"),
        envelope,
        1.5,
        client=Client(*calls),
        prompt_version=PROMPT_V2,
        validator=validator,
        driver=driver,
    )


def test_a_dexi_v2_planner_records_its_version_and_drops_the_claim_under_validator_none() -> None:
    p = v2(("take_off", {"alt_m": 1.0}), ("hold", {"seconds": 2}))
    with pytest.raises(InvalidProposal):
        propose(p, at())
    p.outcome(Outcome(Action("hold", {"seconds": 1.5}), Verdict(True), "done", at(), epochs=1))
    propose(p, at())
    first, second = p.decisions
    assert first.prompt_version == second.prompt_version == "dexi-v2"
    assert "validator" not in first.user_prompt
    assert "choose exactly one action from the provided tools.\n\nMission:" in first.user_prompt
    # Nothing checks under `none`, so the adapter's own rejections don't claim a validator either.
    assert "2. take_off(alt_m=1.0) -> REJECTED: already in the air" in second.user_prompt

    checked = v2(("take_off", {"alt_m": 1.0}), validator="geometric")
    with pytest.raises(InvalidProposal):
        propose(checked, at())
    assert "A safety validator checks every\naction;" in checked.decisions[0].user_prompt
    assert checked.history[-1] == "2. take_off(alt_m=1.0) -> REJECTED by validator: already in the air"


def test_the_freeze_v2_driver_flies_the_decision_time_target() -> None:
    p = planner(("move", {"direction": "forward", "distance_m": 2.0}), ("rotate", {"deg": 90}))
    assert p.arrival == Arrival(position_m=0.25, yaw_deg=5.0, poll_s=None, vertical=False)
    move = propose(p, at(north=1.75, east=0.0, heading=0.0))
    assert move == Action("move", {"north_m": 3.75, "east_m": 0.0})
    assert p.at_execution(move, at(north=1.99, east=0.0, heading=0.0)) is move
    done(p, move)
    turn = propose(p, at(heading=84.0))
    assert p.at_execution(turn, at(heading=89.5)) is turn
    assert (
        p.history[-1]
        == "2. move(direction=forward, distance_m=2.0) -> reached: reached (3.8 N, 0.0 E, 1.5 m alt)"
    )


def test_the_dexi_driver_anchors_relative_primitives_when_they_execute() -> None:
    p = v2(
        ("move", {"direction": "forward", "distance_m": 2.0}),
        ("rotate", {"deg": 90}),
        ("move", {"direction": "up", "distance_m": 0.3}),
        ("hold", {"seconds": 10}),
        driver="dexi",
    )
    assert p.arrival == DEXI_ARRIVAL == Arrival(position_m=0.3, yaw_deg=8.0, poll_s=0.5, vertical=True)
    # Decided 0.25 m short of the last target, still moving; executed 2 s later, settled at it.
    move = propose(p, at(north=1.75, east=0.0, heading=0.0))
    assert move == Action("move", {"north_m": 3.75, "east_m": 0.0})  # what the validator checks
    flown = p.at_execution(move, at(north=1.996, east=0.004, heading=0.04))
    # From the state as MavsdkDriver rounds it: north 2.0, east 0.0, yaw 0.0.
    assert flown == Action("move", {"north_m": 4.0, "east_m": 0.0})
    assert p.timeout_s(flown) == 14.0
    done(p, flown, obs=at(north=3.8, east=0.0, heading=0.0))
    assert (
        p.history[-1]
        == "2. move(direction=forward, distance_m=2.0) -> reached: reached (4.0 N, 0.0 E, 1.5 m alt)"
    )
    turn = propose(p, at(heading=84.0))
    assert turn == Action("rotate", {"heading_deg": 174.0})
    assert p.at_execution(turn, at(heading=89.5)) == Action("rotate", {"heading_deg": 179.5})
    done(p, turn, obs=at(heading=176.0))
    climb = propose(p, at(altitude=1.4))
    assert p.at_execution(climb, at(altitude=1.5)) == Action("take_off", {"altitude_m": 1.8})
    done(p, climb)
    hold = propose(p, at())
    assert p.at_execution(hold, at(north=9.0)) is hold
    # An action that isn't the pending one (the validator rejected it, say) is left alone.
    assert p.at_execution(move, at()) is move


def test_dexi_options_are_checked_before_a_sweep_flies_them() -> None:
    check_options({"model": "claude-sonnet-4-6", "prompt": "dexi-v2", "driver": "dexi"})
    check_options({})
    with pytest.raises(ValueError, match="unknown dexi planner options"):
        check_options({"promt": "dexi-v2"})
    with pytest.raises(ValueError, match="no DEXI driver 'mavsdk'"):
        check_options({"driver": "mavsdk"})
    with pytest.raises(ValueError, match="no DEXI prompt version"):
        resolve_arm("dexi/claude-sonnet-4-6", {"prompt": "v2"})
    with pytest.raises(ValueError, match="planner option"):
        resolve_arm("dexi/claude-sonnet-4-6", {"prompt": "Dexi V2"})


def test_a_variant_arm_is_named_by_its_options() -> None:
    options = {"prompt": "dexi-v2", "driver": "dexi"}
    planner_name, resolved = resolve_arm("dexi/claude-sonnet-4-6", options)
    assert resolved == {**options, "model": "claude-sonnet-4-6"}
    assert arm_name(planner_name, resolved) == "dexi-claude-sonnet-4-6.driver-dexi.prompt-dexi-v2"
    assert arm_name("dexi", {"model": "claude-sonnet-4-6"}) == "dexi-claude-sonnet-4-6"  # as E4 named it
    assert arm_name("scripted-naive", {}) == "scripted-naive"


def test_make_planner_takes_the_options_and_the_agents_validator() -> None:
    scenario = load_scenario(E2 / "b00_healthy.yaml")
    p = make_planner("claude-sonnet-4-6", scenario, validator="none", prompt="dexi-v2", driver="dexi")
    assert (p.prompt_version, p.arrival, "validator" in p.prompt) == ("dexi-v2", DEXI_ARRIVAL, False)
    frozen = make_planner("claude-sonnet-4-6", scenario)
    assert (frozen.prompt_version, frozen.driver, frozen.prompt) == ("dexi-9b2e7a7", "freeze-v2", PROMPT)


def test_the_agent_hands_a_dexi_arm_its_validator_arm() -> None:
    from lost_in_place.agent import AgentConfig
    from lost_in_place.agent import make_planner as agent_planner

    scenario = load_scenario(E2 / "b00_healthy.yaml")
    options = {"model": "claude-sonnet-4-6", "prompt": "dexi-v2"}
    for validator, claims in (("none", False), ("geometric", True)):
        p = agent_planner(AgentConfig(planner="dexi", planner_options=options, validator=validator), scenario)
        assert isinstance(p, DexiPlanner) and ("A safety validator" in p.prompt) == claims
