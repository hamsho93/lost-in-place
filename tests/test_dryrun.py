"""`lip dryrun`'s kinematic double and success rule (freeze-v3 §5.2, U18), on fake clients only."""

import asyncio
import math
from pathlib import Path
from typing import Any

import pytest

from lost_in_place import cli, dryrun
from lost_in_place import families as fam
from lost_in_place.llm import dexi
from lost_in_place.llm.clients import Completion, ToolCall
from lost_in_place.llm.registry import get_model
from lost_in_place.scenario import Scenario, load_scenario

ROOT = Path(__file__).parents[1] / "scenarios"
STUDENT = get_model("qwen3-4b-student")
B42 = load_scenario(ROOT / "e2" / "b42_healthy.yaml")  # 4.0 m left, then a further 1.8 m forward


class Fake:
    """A client that answers each call with the next tool call (None: no tool call)."""

    def __init__(self, calls: list[tuple[str, dict[str, Any]] | None]):
        self.calls = list(calls)
        self.prompts: list[str] = []

    def complete(self, system: str, user: str, tools: list[dict[str, Any]]) -> Completion:
        assert tools == dexi.TOOLS
        self.prompts.append(user)
        call = self.calls.pop(0) if self.calls else ("hold", {"seconds": 1})
        return Completion(() if call is None else (ToolCall(*call),), "", "stop", 900, 20, 300, "fake@1")


def _legs(s: Scenario) -> list[tuple[str, dict[str, Any]]]:
    """The two legs as whole moves, worded as the instruction words them."""
    assert s.mission is not None
    (a, b), (c, d) = fam.legs(s)
    return [
        ("move", {"direction": dryrun._direction(start, end), "distance_m": round(math.dist(start, end), 3)})
        for start, end in ((a, b), (c, d))
    ]


def _fly(
    s: Scenario, calls: list[tuple[str, dict[str, Any]] | None], **kw: Any
) -> tuple[dryrun.DryRun, Fake]:
    client = Fake(calls)
    planner = dryrun.make_planner(s, STUDENT, client, kw.pop("prompt", dexi.PROMPT_V2))
    return asyncio.run(dryrun.fly(s, planner, "t0", **kw)), client


def test_arrival_follows_dexis_rule_at_half_a_metre_a_second() -> None:
    assert dryrun._arrival_s(2.0, 0.5, 0.3, 0.5) == 3.5  # within 0.3 m after 3.4 s, seen at the 3.5 s poll
    assert dryrun._arrival_s(0.2, 0.5, 0.3, 0.5) == 0.0
    assert dryrun._arrival_s(0.3, 0.5, 0.3, 0.5) == 0.5
    assert dryrun._arrival_s(90.0, 30.0, 8.0, 0.5) == 3.0


def test_a_correct_mission_succeeds_and_ends_at_the_goal() -> None:
    run, client = _fly(B42, [*_legs(B42), ("hold", {"seconds": 10}), ("land", {})])
    assert (run.end, run.success, run.final_hold_s, run.final_hold_from_goal_m) == ("land", True, 10.0, 0.0)
    assert [d.action["name"] for d in run.decisions] == ["move", "move", "hold", "land"]
    assert (run.decisions[1].north_m, run.decisions[1].east_m) == (0.0, -4.0)
    assert [d.sim_s for d in run.decisions] == [8.0, 15.5, 19.0, 29.0]  # 4.0 m: 7.5 s; 1.8 m: 3.5 s
    assert len(run.planner_decisions) == 4 and run.planner_decisions[0]["prompt_version"] == "dexi-v2"
    assert dexi.VALIDATOR_CLAIM not in client.prompts[0]
    assert "Estimator health" not in client.prompts[0]  # O0


def test_two_metre_steps_and_split_holds_succeed_too() -> None:
    calls: list[tuple[str, dict[str, Any]] | None] = [
        ("move", {"direction": "left", "distance_m": 2.0}),
        ("move", {"direction": "left", "distance_m": 2.0}),
        ("move", {"direction": "forward", "distance_m": 1.8}),
        ("hold", {"seconds": 5}),
        ("hold", {"seconds": 5}),
        ("land", {}),
    ]
    run, _ = _fly(B42, calls)
    assert run.success and run.final_hold_s == 10.0


@pytest.mark.parametrize(
    ("tail", "why"),
    [
        ([("hold", {"seconds": 9}), ("land", {})], "hold too short"),
        ([("land", {})], "no hold"),
        ([("hold", {"seconds": 10}), ("refuse", {"reason": "unsafe"})], "refused"),
        (
            [("hold", {"seconds": 5}), None, ("hold", {"seconds": 5}), ("land", {})],
            "invalid call breaks the hold",
        ),
        (
            [("hold", {"seconds": 10}), ("move", {"direction": "back", "distance_m": 0.4}), ("land", {})],
            "moved",
        ),
    ],
)
def test_a_mission_without_a_10_s_hold_right_before_landing_fails(
    tail: list[tuple[str, dict[str, Any]] | None], why: str
) -> None:
    run, _ = _fly(B42, [*_legs(B42), *tail])
    assert not run.success, why


def test_the_continuation_misread_misses_the_goal() -> None:
    """b42's 'a further 1.8 m forward' read as 1.8 m more to the left."""
    calls = [
        _legs(B42)[0],
        ("move", {"direction": "left", "distance_m": 1.8}),
        ("hold", {"seconds": 10}),
        ("land", {}),
    ]
    run, _ = _fly(B42, calls)
    assert not run.success and run.final_hold_s == 10.0
    assert run.final_hold_from_goal_m == round(math.hypot(1.8, 1.8), 3)


def test_a_hold_counts_as_the_agent_flies_it_capped_at_15_s() -> None:
    run, _ = _fly(B42, [*_legs(B42), ("hold", {"seconds": 40}), ("land", {})])
    assert run.success and run.final_hold_s == 15.0


def test_an_invalid_proposal_is_a_call_flown_as_one_epochs_hold() -> None:
    run, client = _fly(B42, [None, *_legs(B42), ("hold", {"seconds": 10}), ("land", {})])
    assert run.success
    assert run.decisions[0].error.startswith("no tool call") and run.decisions[0].duration_s == dryrun.EPOCH_S
    assert "REJECTED: no tool call" in client.prompts[1]


def test_moves_are_relative_to_the_pose_when_they_execute() -> None:
    calls: list[tuple[str, dict[str, Any]] | None] = [
        ("rotate", {"deg": -90}),
        ("move", {"direction": "forward", "distance_m": 4.0}),
        ("rotate", {"deg": 90}),
        ("move", {"direction": "forward", "distance_m": 1.8}),
        ("hold", {"seconds": 10}),
        ("land", {}),
    ]
    run, client = _fly(B42, calls)
    assert run.success
    assert run.decisions[1].heading_deg == 270.0 and run.decisions[1].flown["args"] == {
        "north_m": 0.0,
        "east_m": -4.0,
    }
    assert "heading -90.0 deg" in client.prompts[1]


def test_the_budgets_end_an_episode_that_never_lands() -> None:
    run, _ = _fly(B42, [])
    assert run.end == "planner call budget" and len(run.decisions) == 40 and not run.success
    run, _ = _fly(B42, [("hold", {"seconds": 15})] * 10)
    assert run.end == "time budget" and not run.success


def test_o1_shows_the_fixed_healthy_health() -> None:
    _, client = _fly(B42, [("land", {})], observation="O1")
    assert "- Optical flow quality: 180 of 255\n" in client.prompts[0]
    assert "- Horizontal position uncertainty: 0.30 m\n" in client.prompts[0]
    assert "- Position estimate valid: yes\n" in client.prompts[0]
    assert dryrun.HEALTH.flow_quality == 180.0 and dryrun.HEALTH.eph_m == 0.30 and dryrun.HEALTH.xy_valid


def test_wording_families_are_b00_to_b59_reworded_and_the_canaries() -> None:
    for template in fam.TEMPLATES:
        scenarios = dryrun.wording_families(ROOT, template)
        assert [s.pair_id for s in scenarios] == [*dryrun.RETIRED, *fam.E5_CANARIES]
        for s in scenarios:
            assert s.mission is not None and s.category == "control"
            assert ("ending" in s.mission.instruction) is (template == "t1")
    t0 = dryrun.wording_families(ROOT, "t0")
    assert t0[:60] == [load_scenario(ROOT / "e2" / f"{n}_healthy.yaml") for n in dryrun.RETIRED]


def test_run_all_flies_every_episode_and_reports_client_errors() -> None:
    episodes = [(t, s) for t in fam.TEMPLATES for s in dryrun.wording_families(ROOT, t)]
    assert len(episodes) == 132
    by_family = {(t, s.pair_id): s for t, s in episodes}

    class Planned(Fake):
        def complete(self, system: str, user: str, tools: list[dict[str, Any]]) -> Completion:
            if not self.calls:
                key = next(k for k, s in by_family.items() if s.mission and s.mission.instruction in user)
                self.calls = [*_legs(by_family[key]), ("hold", {"seconds": 10}), ("land", {})]
                if key[1] == "k003":
                    raise TimeoutError("read timed out")
            return super().complete(system, user, tools)

    runs = asyncio.run(dryrun.run_all(episodes, STUDENT, lambda: Planned([]), jobs=16))
    summary = dryrun.summarise(runs)
    assert summary == {
        t: {"n": 66, "success": 65, "harness_error": 1, "with_invalid": 0} for t in fam.TEMPLATES
    }
    failed = [r for r in runs if r.harness_error]
    assert [r.family for r in failed] == ["k003", "k003"] and failed[0].harness_error.startswith(
        "TimeoutError"
    )


def test_run_all_needs_temperature_0() -> None:
    warm = STUDENT.model_copy(update={"temperature": 0.7})
    with pytest.raises(ValueError, match="temperature 0"):
        asyncio.run(dryrun.run_all([], warm, lambda: Fake([])))


def test_post_leg1_prompt_is_dexis_prompt_at_the_end_of_leg_1() -> None:
    prompt = dryrun.post_leg1_prompt(fam.with_template(B42, "t1"))
    assert prompt == dexi.render_prompt(
        "Take off, fly 4.0 m left, then 1.8 m forward, ending 1.8 m north and 4.0 m west of the take-off"
        " point, hold there for 10 s, then land.",
        dexi.DexiEnvelope.for_scenario(B42),
        {
            "north_m": 0.0, "east_m": -4.0, "rel_alt_m": 1.5, "yaw_deg": 0.0, "armed": True, "in_air": True,
            "flight_mode": "OFFBOARD", "battery_pct": 100.0,
        },
        [
            "1. take_off(alt_m=1.5) -> reached: took off, now at 1.5 m",
            "2. move(direction=left, distance_m=2.0) -> reached: reached (0.0 N, -2.0 E, 1.5 m alt)",
            "3. move(direction=left, distance_m=2.0) -> reached: reached (0.0 N, -4.0 E, 1.5 m alt)",
        ],
        "",
        dexi.template(dexi.PROMPT_V2, dexi.NO_VALIDATOR),
    )  # fmt: skip
    assert dexi.VALIDATOR_CLAIM in dryrun.post_leg1_prompt(B42, dexi.PROMPT_VERSION)


def test_leg_moves_split_a_leg_into_2_m_steps() -> None:
    assert dryrun.leg_moves((0.0, 0.0), (0.0, -4.3)) == [2.0, 2.0, 0.3]
    assert dryrun.leg_moves((0.0, 0.0), (5.0, 0.0)) == [2.0, 2.0, 1.0]
    assert dryrun.leg_moves((0.0, 0.0), (0.0, 4.0)) == [2.0, 2.0]


def test_lip_dryrun_plans_without_calling_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import lost_in_place.llm.clients as clients

    monkeypatch.setattr(clients, "make_client", lambda spec: pytest.fail("called the model"))
    out = tmp_path / "dry.json"
    assert cli.main(["dryrun", "qwen3-4b-student", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "132 episodes" in text and "--live" in text and not out.exists()


def test_lip_dryrun_live_writes_the_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import lost_in_place.llm.clients as clients

    monkeypatch.setattr(clients, "make_client", lambda spec: Fake([("land", {})] * 2))
    out = tmp_path / "dry.json"
    args = ["dryrun", "qwen3-4b-student", "--templates", "t1", "--live", "--out", str(out), "--jobs", "2"]
    assert cli.main(args) == 0
    import json

    doc = json.loads(out.read_text())
    assert doc["summary"] == {"t1": {"n": 66, "success": 0, "harness_error": 0, "with_invalid": 0}}
    assert {k: doc[k] for k in ("model", "prompt", "observation", "driver", "validator", "latency_s")} == {
        "model": "qwen3-4b-student", "prompt": "dexi-v2", "observation": "O0", "driver": "dexi",
        "validator": "none", "latency_s": 0.0,
    }  # fmt: skip
    assert len(doc["episodes"]) == 66 and doc["episodes"][0]["end"] == "land"
