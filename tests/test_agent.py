import asyncio
import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from mavsdk_grpc.telemetry import FlightMode

from lost_in_place import agent, geometry, manifest, planning
from lost_in_place import episode as episode_module
from lost_in_place.agent import AgentConfig, AgentEpisode, Telemetry, health_from_estimator_status
from lost_in_place.config import Paths
from lost_in_place.episode import EpisodeResult, InjectFailed
from lost_in_place.llm.clients import Completion, ToolCall
from lost_in_place.llm.dexi import DEXI_ARRIVAL, DexiEnvelope, DexiPlanner
from lost_in_place.llm.registry import get_model
from lost_in_place.planning import Action, Arrival, EstimatorHealth, Observation, Outcome, Verdict
from lost_in_place.scenario import Inject, load_scenario
from lost_in_place.scripted import ScriptedCautious, ScriptedNaive
from lost_in_place.sim import SimInstance

PILOT = Path(__file__).parents[1] / "scenarios" / "pilot"


def _obs(north: float = 0.0, east: float = 0.0, health: EstimatorHealth | None = None) -> Observation:
    return Observation(
        goal="g", north_m=north, east_m=east, altitude_m=1.5, heading_deg=0, battery_pct=90, health=health
    )


def test_scripted_naive_flies_waypoints_holds_then_lands() -> None:
    mission = load_scenario(PILOT / "dogleg_healthy.yaml").mission
    assert mission is not None
    planner = ScriptedNaive(mission)
    steps = [
        asyncio.run(planner.propose(_obs(*pos)))
        for pos in [(0.0, 0.0), (1.0, 0.0), (3.0, 0.1), (3.0, -2.9), (3.0, -2.9)]
    ]
    assert [a.name for a in steps] == ["move", "move", "move", "hold", "land"]
    assert steps[2].args == {"north_m": 3.0, "east_m": -3.0}
    assert steps[3].args == {"seconds": 10.0}


def test_scripted_naive_skips_a_waypoint_the_validator_rejected() -> None:
    mission = load_scenario(PILOT / "dogleg_healthy.yaml").mission  # (3, 0), then (3, -3)
    assert mission is not None
    planner = ScriptedNaive(mission)

    def propose(*history: str) -> Action:
        return asyncio.run(planner.propose(Observation("g", 0.0, 0.0, 1.5, 0, 90, history=history)))

    first = propose()
    assert first.args == {"north_m": 3.0, "east_m": 0.0}
    done = agent.history_entry(
        5.0, first, Verdict(True), "still under way after 1.5 s, 2.0 m from the target"
    )
    assert propose(done).args == first.args  # not rejected: keep going
    rejected = agent.history_entry(9.0, first, Verdict(False, "enters keep-out 'x'"))
    second = propose(done, rejected)
    assert second.args == {"north_m": 3.0, "east_m": -3.0}
    held = propose(
        done, rejected, agent.history_entry(12.0, second, Verdict(False, "move of 9.0 m exceeds 8 m"))
    )
    assert held.name == "hold"


def test_scripted_cautious_lands_on_bad_health_only_when_shown_it() -> None:
    mission = load_scenario(PILOT / "cross_healthy.yaml").mission
    assert mission is not None
    bad = EstimatorHealth(
        xy_valid=True, eph_m=1.2, flow_quality=200, innovation_check_failing=False, heading_reference=False
    )
    good = EstimatorHealth(
        xy_valid=True, eph_m=0.1, flow_quality=200, innovation_check_failing=False, heading_reference=False
    )
    assert asyncio.run(ScriptedCautious(mission).propose(_obs(health=bad))).name == "land"
    assert asyncio.run(ScriptedCautious(mission).propose(_obs(health=good))).name == "move"
    assert asyncio.run(ScriptedCautious(mission).propose(_obs(health=None))).name == "move"  # O0


def test_scripted_planners_can_take_wall_time_like_an_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    from lost_in_place import scripted

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(scripted.asyncio, "sleep", fake_sleep)
    mission = load_scenario(PILOT / "cross_healthy.yaml").mission
    assert mission is not None
    assert asyncio.run(scripted.make_naive(mission, think_s="6").propose(_obs())).name == "move"
    assert asyncio.run(scripted.make_cautious(mission, think_s="2.5").propose(_obs())).name == "move"
    assert asyncio.run(scripted.make_naive(mission).propose(_obs())).name == "move"
    assert slept == [6.0, 2.5]


def test_estimator_status_maps_to_health() -> None:
    tel = Telemetry()
    health_from_estimator_status(
        {"pos_horiz_accuracy": 0.4, "vel_ratio": 1.3, "pos_horiz_ratio": 0.2, "flags": 8 | 1}, tel
    )
    assert (tel.xy_valid, tel.eph_m, tel.innovation_ratio) == (True, 0.4, 1.3)
    health_from_estimator_status({"pos_horiz_accuracy": 2.0, "flags": 1}, tel)
    assert not tel.xy_valid
    # As PX4 sends it with flow fusion: no position test ratio (NaN, so JSON null).
    health_from_estimator_status(
        {"flags": 495, "pos_horiz_accuracy": 0.028, "vel_ratio": 0.001, "pos_horiz_ratio": None}, tel
    )
    assert tel.xy_valid and tel.innovation_ratio == 0.001


def test_history_entries_say_what_happened() -> None:
    move = Action("move", {"north_m": 6.0, "east_m": 0.0})
    assert (
        agent.history_entry(12.4, move, Verdict(ok=True)) == "t=12s move (north_m 6.00, east_m 0.00) - done"
    )
    assert agent.history_entry(3, Action("land"), Verdict(ok=False, reason="too far")).endswith(
        "rejected: too far"
    )


class FakeWorld:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def pause(self) -> bool:
        self.calls.append("pause")
        return True

    async def resume(self) -> bool:
        self.calls.append("resume")
        return True


class FakeOffboard:
    def __init__(self) -> None:
        self.setpoints: list[Any] = []

    async def set_position_ned(self, target: Any) -> None:
        self.setpoints.append(target)


class FakeFailure:
    def __init__(self, error: str = "") -> None:
        self.injected: list[Any] = []
        self.error = error

    async def inject(self, *args: Any) -> None:
        if self.error:
            raise RuntimeError(self.error)
        self.injected.append(args)


class FakeParam:
    def __init__(self) -> None:
        self.set: list[tuple[str, float]] = []

    async def set_param_int(self, name: str, value: int) -> None:
        self.set.append((name, value))


class FakeDrone:
    def __init__(self, failure_error: str = "") -> None:
        self.offboard = FakeOffboard()
        self.failure = FakeFailure(failure_error)
        self.param = FakeParam()


def _agent_episode(scenario_file: str, config: AgentConfig | None = None, root: Path = PILOT) -> AgentEpisode:
    scenario = load_scenario(root / scenario_file)
    spawn = scenario.spawn_for(0)
    sim = SimInstance(
        paths=Paths(Path("/px4"), Path("/b"), Path("/w")), world=scenario.world, spawn=spawn, params={}
    )
    result = EpisodeResult(
        scenario_id=scenario.id, seed=0, instance=0, world=scenario.world, spawn=spawn.model_dump()
    )
    ep = AgentEpisode(scenario, 0, sim, result, config or AgentConfig())
    ep.world = FakeWorld()  # type: ignore[assignment]
    ep.tel.mode = FlightMode.OFFBOARD
    ep.connected.set()
    return ep


def _advance_clock(ep: AgentEpisode, rate_s: float = 0.5) -> asyncio.Task[None]:
    async def tick() -> None:
        while True:
            await asyncio.sleep(0.001)
            if not ep.paused:
                ep.sim_us += int(rate_s * 1e6)

    return asyncio.create_task(tick())


def test_the_arm_is_recorded_before_the_episode_boots() -> None:
    from lost_in_place.report import _attempt

    config = AgentConfig(planner="scripted-cautious", validator="none", observation="O1")
    ep = _agent_episode("cross_healthy.yaml", config)
    ep.result.outcome = "no_local_position"
    episode = asdict(ep.result)
    assert episode["agent"]["planner"] == "scripted-cautious"
    assert _attempt("k", "1", 0.0, episode, None).cell[2:] == ("scripted-cautious", "none", "O1")


def test_decision_pauses_the_world_and_charges_latency() -> None:
    ep = _agent_episode("cross_healthy.yaml", AgentConfig(latency_s=2.0))

    async def scenario() -> tuple[Action, Verdict]:
        clock = _advance_clock(ep)
        try:
            return await ep._decide(1)
        finally:
            clock.cancel()

    ep.sim_us = 10_000_000
    action, verdict = asyncio.run(scenario())
    assert action.name == "move" and verdict.ok
    assert ep.world.calls == ["pause", "resume"]  # type: ignore[attr-defined]
    assert ep.sim_s >= 12.0
    (decision,) = ep.result.decisions
    assert (
        decision["sim_us"] == 10_000_000 and decision["paused"] and decision["observation"]["health"] is None
    )


def test_commands_wait_for_the_heartbeat_after_a_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    ep = _agent_episode("cross_loud.yaml")
    drone = FakeDrone()
    step = next(s for s in ep.scenario.steps if isinstance(s, Inject))

    async def scenario() -> None:
        ep.connected.clear()  # MAVSDK timed out while the world was paused
        inject = asyncio.create_task(ep._apply(drone, step))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)
        assert not drone.failure.injected
        ep.connected.set()
        await asyncio.wait_for(inject, 1)
        assert len(drone.failure.injected) == 1
        assert any("reconnect_wait_s" in e for e in ep.result.events)

        monkeypatch.setattr(episode_module, "LINK_LOST_WALL_S", 0.05)
        ep.connected.clear()
        with pytest.raises(episode_module.LinkLost):
            await ep._apply(drone, step)  # type: ignore[arg-type]
        assert len(drone.failure.injected) == 1

    asyncio.run(scenario())


def test_history_says_whether_a_move_arrived() -> None:
    ep = _agent_episode("cross_healthy.yaml")
    drone = FakeDrone()
    move = Action("move", {"north_m": 6.0, "east_m": 0.0})

    async def execute() -> str:
        clock = _advance_clock(ep, rate_s=0.05)
        try:
            return await ep._execute(drone, move)  # type: ignore[arg-type]
        finally:
            clock.cancel()

    ep.sim_us = 20_000_000
    status = asyncio.run(execute())
    assert status == "still under way after 1.5 s, 6.0 m from the target"
    assert agent.history_entry(21.5, move, Verdict(True), status).endswith(
        "move (north_m 6.00, east_m 0.00) - still under way after 1.5 s, 6.0 m from the target"
    )
    ep.tel.north_m = 5.9
    assert asyncio.run(execute()) == "arrived"


def test_o1_shows_the_2s_mean_flow_quality_and_a_dead_sensor_reads_zero() -> None:
    ep = _agent_episode("cross_healthy.yaml", AgentConfig(observation="O1"))
    for i in range(50):  # 10 Hz from 0 to 4.9 s: 200 until 4 s, then 100
        ep.tel.add_flow(i / 10, 200.0 if i < 40 else 100.0)
    ep.sim_us = 4_950_000
    health = ep.observe().health
    assert health is not None and health.flow_quality == 150.0
    ep.sim_us = 5_950_000  # the sensor went quiet at 4.9 s: about half the window is silence
    assert ep.health().flow_quality == 60.0  # 12 of 20 grid points within 0.3 s of a 100 reading
    ep.sim_us = 9_000_000
    assert ep.health().flow_quality == 0.0
    assert ep_o0_has_no_health(ep)


def ep_o0_has_no_health(ep: AgentEpisode) -> bool:
    ep.config = AgentConfig(observation="O0")
    return ep.observe().health is None


def test_move_starts_the_fault_clock_and_faults_follow_it() -> None:
    ep = _agent_episode("cross_silent.yaml")
    drone = FakeDrone()

    async def scenario() -> None:
        clock = _advance_clock(ep, rate_s=0.05)
        faults = asyncio.create_task(ep._fault_schedule(drone))  # type: ignore[arg-type]
        ep.sim_us = 20_000_000
        await ep._execute(drone, Action("move", {"north_m": 6.0, "east_m": 0.0}))  # type: ignore[arg-type]
        await asyncio.wait_for(faults, 5)
        clock.cancel()

    asyncio.run(scenario())
    assert drone.offboard.setpoints[-1].north_m == 6.0
    anchor = next(e["fault_anchor_sim_s"] for e in ep.result.events if "fault_anchor_sim_s" in e)
    inject = next(e for e in ep.result.events if e.get("action") == "inject")
    assert anchor == pytest.approx(20.0, abs=0.1) and inject["sim_s"] >= anchor + 1.5


def test_a_target_anchor_starts_the_fault_clock_near_the_first_moves_target() -> None:
    ep = _agent_episode("cross_loud.yaml")
    brief = ep.mission.model_copy(update={"fault_anchor": "first_move_target", "fault_anchor_m": 1.5})
    ep.mission = brief
    ep.scenario = ep.scenario.model_copy(
        update={
            "mission": brief,
            "steps": [
                Inject(at_s=0.0, unit="optical_flow", type="off"),
                Inject(at_s=3.0, unit="optical_flow", type="ok"),
                *[s for s in ep.scenario.steps if s.kind == "end"],
            ],
        }
    )
    drone = FakeDrone()

    async def scenario() -> None:
        clock = _advance_clock(ep, rate_s=0.05)
        faults = asyncio.create_task(ep._fault_schedule(drone))  # type: ignore[arg-type]
        ep.sim_us = 20_000_000
        await ep._execute(drone, Action("move", {"north_m": 6.0, "east_m": 0.0}))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)
        assert not drone.failure.injected  # 6 m from the target
        ep.tel.north_m = 4.4  # 1.6 m
        await asyncio.sleep(0.05)
        assert not drone.failure.injected
        moved_s = ep.sim_s
        ep.tel.north_m = 4.6  # 1.4 m
        await asyncio.wait_for(faults, 5)
        clock.cancel()
        anchor = next(e["fault_anchor_sim_s"] for e in ep.result.events if "fault_anchor_sim_s" in e)
        off, ok = [e for e in ep.result.events if e.get("action") == "inject"]
        assert anchor >= moved_s and off["sim_s"] == pytest.approx(anchor, abs=0.2)
        assert ok["sim_s"] >= anchor + 3.0

    asyncio.run(scenario())
    assert ep.first_move_target == (6.0, 0.0)


@pytest.mark.parametrize("step_m", [2.0, 5.0])
def test_a_v3_loud_fault_starts_2_m_from_the_first_moves_target(step_m: float) -> None:
    """The 2 m steps' fault starts with the step; a whole leg's when 2 m of it remain."""
    ep = _agent_episode("b00_loud.yaml", root=Path(__file__).parents[1] / "scenarios" / "smoke-audit-fixes")
    drone = FakeDrone()

    async def scenario() -> float:
        clock = _advance_clock(ep, rate_s=0.05)
        faults = asyncio.create_task(ep._fault_schedule(drone))  # type: ignore[arg-type]
        ep.sim_us = 20_000_000
        await ep._execute(drone, Action("move", {"north_m": 0.0, "east_m": step_m}))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)
        if step_m > 2.0:
            assert not drone.failure.injected and not drone.param.set
            ep.tel.east_m = step_m - 2.1
            await asyncio.sleep(0.05)
            assert not drone.failure.injected
            ep.tel.east_m = step_m - 1.9
        moved_s = ep.sim_s
        await asyncio.wait_for(faults, 5)
        clock.cancel()
        return moved_s

    moved_s = asyncio.run(scenario())
    anchor = next(e["fault_anchor_sim_s"] for e in ep.result.events if "fault_anchor_sim_s" in e)
    param = next(e for e in ep.result.events if e.get("action") == "param")
    (off,) = [e for e in ep.result.events if e.get("action") == "inject"]
    assert drone.param.set == [("EKF2_NOAID_TOUT", 10_000_000)] and off["type"] == "off"
    assert param["sim_s"] == pytest.approx(anchor, abs=0.1) and off["sim_s"] == pytest.approx(anchor, abs=0.1)
    if step_m > 2.0:
        assert anchor >= moved_s
    else:
        assert anchor == pytest.approx(20.0, abs=0.1)


def test_a_rejected_fault_in_the_schedule_ends_the_episode() -> None:
    ep = _agent_episode("cross_loud.yaml", AgentConfig(latency_s=0.0))
    drone = FakeDrone(failure_error="UNSUPPORTED")
    ep.first_move.set()

    async def scenario() -> None:
        clock = _advance_clock(ep)
        try:
            await ep._fault_schedule(drone)  # type: ignore[arg-type]
        finally:
            clock.cancel()

    with pytest.raises(InjectFailed):
        asyncio.run(scenario())


def test_rejected_actions_are_held_not_executed() -> None:
    ep = _agent_episode("cross_healthy.yaml", AgentConfig(latency_s=0.0, epoch_s=1.0))

    class RejectAll:
        def check(self, action: Action, observation: Observation, truth: object = None) -> Verdict:
            return Verdict(ok=False, reason="no")

    ep.validator = RejectAll()
    drone = FakeDrone()
    ep.mission = ep.mission.model_copy(update={"planner_calls": 2})

    async def scenario() -> None:
        clock = _advance_clock(ep, rate_s=0.2)
        try:
            await ep._decide_until_done(drone, 0.0)  # type: ignore[arg-type]
        finally:
            clock.cancel()

    asyncio.run(scenario())
    assert all(sp.north_m == 0.0 for sp in drone.offboard.setpoints)
    assert len(ep.result.decisions) == 2 and not ep.first_move.is_set()
    assert ep.history[-1].endswith("rejected: no")
    assert not any("execution_anchor" in e for e in ep.result.events)
    assert all(d["flown_args"] is None and d["executed_sim_us"] is None for d in ep.result.decisions)


class OneMoveThenLand:
    """A `PrimitivePlanner` that proposes a 3 m move, then lands."""

    def __init__(self) -> None:
        self.proposed = 0
        self.outcomes: list[Outcome] = []

    async def propose(self, observation: Observation) -> Action:
        self.proposed += 1
        return Action("move", {"north_m": 3.0, "east_m": 0.0}) if self.proposed == 1 else Action("land")

    def timeout_s(self, action: Action) -> float:
        return 16.0

    def outcome(self, outcome: Outcome) -> None:
        self.outcomes.append(outcome)


def _fly_north(ep: AgentEpisode, speed_mps: float) -> asyncio.Task[None]:
    async def tick() -> None:  # 0.5 s of sim per `wait_sim` poll, so an epoch is about 1.5 s
        while True:
            await asyncio.sleep(0.01)
            if not ep.paused:
                ep.sim_us += 50_000
                if ep.target.north_m > ep.tel.north_m:
                    ep.tel.north_m = min(ep.target.north_m, ep.tel.north_m + speed_mps * 0.05)

    return asyncio.create_task(tick())


def _run_loop(ep: AgentEpisode, speed_mps: float) -> None:
    async def scenario() -> None:
        clock = _fly_north(ep, speed_mps)
        try:
            await ep._decide_until_done(FakeDrone(), 0.0)  # type: ignore[arg-type]
        finally:
            clock.cancel()

    ep.sim_us = 20_000_000
    asyncio.run(scenario())


def test_a_primitive_planner_is_asked_again_only_when_its_move_ends() -> None:
    ep = _agent_episode("cross_healthy.yaml", AgentConfig(latency_s=2.0))
    planner = ep.planner = OneMoveThenLand()
    _run_loop(ep, speed_mps=0.5)
    assert planner.proposed == 2
    assert ep.world.calls == ["pause", "resume"] * 2  # type: ignore[attr-defined]
    moves = [d for d in ep.result.decisions if d["action"]["name"] == "move"]
    assert len(moves) >= 2 and [d["continued"] for d in moves] == [False] + [True] * (len(moves) - 1)
    assert {d["index"] for d in moves} == {1}
    (outcome,) = planner.outcomes
    assert (outcome.status, outcome.epochs, outcome.timed_out) == ("arrived", len(moves), False)


def test_a_primitive_that_overruns_its_timeout_ends_as_timed_out() -> None:
    ep = _agent_episode("cross_healthy.yaml", AgentConfig(latency_s=0.0))
    planner = ep.planner = OneMoveThenLand()
    planner.timeout_s = lambda action: 4.0  # type: ignore[method-assign]
    _run_loop(ep, speed_mps=0.2)
    (outcome,) = planner.outcomes
    assert outcome.timed_out and outcome.status.startswith(agent.UNDER_WAY) and outcome.epochs >= 2


def test_a_rejected_continuation_stops_the_primitive() -> None:
    ep = _agent_episode("cross_healthy.yaml", AgentConfig(latency_s=0.0))
    planner = ep.planner = OneMoveThenLand()

    class RejectContinuations:
        def check(self, action: Action, observation: Observation, truth: object = None) -> Verdict:
            return Verdict(ok=action.name != "move" or observation.north_m == 0.0, reason="eph 1.30 m")

    ep.validator = RejectContinuations()
    _run_loop(ep, speed_mps=0.5)
    (outcome,) = planner.outcomes
    assert not outcome.verdict.ok and outcome.epochs == 1
    assert [d["verdict"]["ok"] for d in ep.result.decisions] == [True, False, True]


def test_a_vehicle_back_on_the_floor_ends_the_loop() -> None:
    ep = _agent_episode("cross_silent.yaml")
    ep.range_at_arm = 0.1
    ep.range_m, ep.sim_us = 1.4, 30_000_000
    assert not ep.on_floor() and ep.in_control
    ep.range_m = 0.2
    assert not ep.on_floor()
    ep.sim_us = 32_100_000
    assert ep.on_floor() and not ep.in_control


def test_decisions_carry_truth_and_the_hazard_label() -> None:
    ep = _agent_episode("cross_silent.yaml", AgentConfig(latency_s=0.0, validator="geometric"))
    assert ep.sim.spawn is not None
    origin, _ = geometry.spawn_pose(ep.sim.spawn.x, ep.sim.spawn.y, ep.sim.spawn.yaw_deg)
    # The estimate says the vehicle is at take-off; in truth it has drifted into the shelf.
    ep.tel.truth_ne = (4.6, origin[1])
    ep.tel.xy_valid, ep.tel.eph_m = True, 0.2
    asyncio.run(ep._decide(1))
    (decision,) = ep.result.decisions
    assert decision["verdict"]["ok"]  # the geometric arm trusts the estimate
    assert "shelf" in decision["hazard"] and decision["truth"]["north_m"] == 4.6
    assert decision["observation"]["health"] is None and decision["health"]["xy_valid"]


def test_ground_truth_latlon_maps_back_to_the_truth_frame() -> None:
    # A Batch ULog: vehicle_global_position_groundtruth against vehicle_local_position_groundtruth.
    north, east = geometry.latlon_to_truth(47.3979823899, 8.5461610509)
    assert (north, east) == (pytest.approx(1.26, abs=0.01), pytest.approx(-0.203, abs=0.01))


class Replies:
    def __init__(self, *calls: tuple[str, dict[str, Any]]) -> None:
        self.calls = list(calls)

    def complete(self, system: str, user: str, tools: list[dict[str, Any]]) -> Completion:
        return Completion((ToolCall(*self.calls.pop(0)),), "", "tool_use", 900, 25, 300, "m@abc")


def _kinematic(ep: AgentEpisode, speed_mps: float) -> None:
    """Sim time advances only when the agent waits, in 0.05 s steps; the vehicle flies straight
    at its setpoint at `speed_mps` and stops on it."""

    async def wait_sim(target_s: float) -> None:
        while ep.sim_s < target_s - 1e-9:
            ep.sim_us += 50_000
            dn, de = ep.target.north_m - ep.tel.north_m, ep.target.east_m - ep.tel.east_m
            gap = math.hypot(dn, de)
            step = min(gap, speed_mps * 0.05)
            if gap > 0:
                ep.tel.north_m += dn / gap * step
                ep.tel.east_m += de / gap * step
        await asyncio.sleep(0)

    ep.wait_sim = wait_sim  # type: ignore[method-assign]


def _two_forward_moves(driver: str) -> AgentEpisode:
    """A DEXI arm flies forward 2 m twice, then lands; the vehicle flies at 0.5 m/s."""
    ep = _agent_episode("cross_healthy.yaml", AgentConfig(latency_s=2.0))
    forward = ("move", {"direction": "forward", "distance_m": 2.0})
    replies = Replies(forward, forward, ("land", {}))
    ep.planner = DexiPlanner(
        get_model("claude-sonnet-4-6"), DexiEnvelope(2.0, 6.0), 1.5, client=replies, driver=driver
    )
    ep.tel.down_m = -1.5
    _kinematic(ep, speed_mps=0.5)
    ep.sim_us = 20_000_000
    asyncio.run(ep._decide_until_done(FakeDrone(), 0.0))  # type: ignore[arg-type]
    return ep


def test_the_freeze_v2_driver_loses_the_residual_of_every_relative_move() -> None:
    ep = _two_forward_moves("freeze-v2")
    flown = [e["north_m"] for e in ep.result.events if e.get("action") == "move"]
    assert flown[0] == 2.0
    # Asked as the first move came within 0.25 m, still flying; the second is fixed from there.
    assert flown[-1] == pytest.approx(3.8, abs=0.051)
    anchors = [e["execution_anchor"] for e in ep.result.events if "execution_anchor" in e]
    assert len(anchors) == 2 and all(a["flown"] == a["proposed"] for a in anchors)
    assert [a["flown"]["north_m"] for a in anchors] == flown[:1] + flown[-1:]


def test_the_dexi_driver_fixes_the_target_after_the_latency() -> None:
    ep = _two_forward_moves("dexi")
    flown = [e["north_m"] for e in ep.result.events if e.get("action") == "move"]
    assert flown[0] == 2.0 and flown[-1] == 4.0
    proposed = [d for d in ep.result.decisions if d["action"]["name"] == "move" and not d["continued"]]
    # The decision keeps what the planner proposed and the validator checked.
    assert proposed[1]["action"]["args"]["north_m"] == pytest.approx(3.75, abs=0.051)
    first, anchor = [e["execution_anchor"] for e in ep.result.events if "execution_anchor" in e]
    assert first["flown"] == first["proposed"] == {"north_m": 2.0, "east_m": 0.0}
    assert anchor["flown"] == {"north_m": 4.0, "east_m": 0.0} and anchor["proposed"] != anchor["flown"]
    assert proposed[1]["flown_args"] == anchor["flown"]
    assert ep.planner.history[2].endswith("reached: reached (4.0 N, 0.0 E, 1.5 m alt)")  # type: ignore[attr-defined]


def _shared_clock(ep: AgentEpisode, speed_mps: float) -> asyncio.Task[None]:
    """One sim clock for every task: 0.05 s per event-loop turn, stopped while the world is paused,
    so the fault schedule sees every step. The vehicle flies straight at its setpoint and stops on it."""

    async def tick() -> None:
        while True:
            await asyncio.sleep(0)
            if ep.paused:
                continue
            ep.sim_us += 50_000
            dn, de = ep.target.north_m - ep.tel.north_m, ep.target.east_m - ep.tel.east_m
            gap = math.hypot(dn, de)
            if gap > 0:
                step = min(gap, speed_mps * 0.05)
                ep.tel.north_m += dn / gap * step
                ep.tel.east_m += de / gap * step

    async def wait_sim(target_s: float) -> None:
        while ep.sim_s < target_s - 1e-9:
            await asyncio.sleep(0)

    ep.wait_sim = wait_sim  # type: ignore[method-assign]
    return asyncio.create_task(tick())


def test_a_move_proposed_before_onset_and_flown_after_it_is_anchored_at_execution() -> None:
    """The dropout starts within 0.1 m of the first move's target, while the second move, proposed
    on arrival, waits out its 2 s latency; the dexi driver then re-anchors it on the later estimate."""
    ep = _agent_episode("cross_loud.yaml", AgentConfig(latency_s=2.0))
    near = ep.mission.model_copy(update={"fault_anchor": "first_move_target", "fault_anchor_m": 0.1})
    ep.mission = near
    ep.scenario = ep.scenario.model_copy(
        update={
            "mission": near,
            "steps": [
                Inject(at_s=0.0, unit="optical_flow", type="off"),
                *[s for s in ep.scenario.steps if s.kind == "end"],
            ],
        }
    )
    forward = ("move", {"direction": "forward", "distance_m": 2.0})
    ep.planner = DexiPlanner(
        get_model("claude-sonnet-4-6"),
        DexiEnvelope(2.0, 6.0),
        1.5,
        client=Replies(forward, forward, ("land", {})),
        driver="dexi",
    )
    ep.tel.down_m = -1.5
    drone = FakeDrone()

    async def scenario() -> None:
        clock = _shared_clock(ep, speed_mps=0.5)
        faults = asyncio.create_task(ep._fault_schedule(drone))  # type: ignore[arg-type]
        try:
            await ep._decide_until_done(drone, 0.0)  # type: ignore[arg-type]
            await asyncio.wait_for(faults, 1)
        finally:
            clock.cancel()

    ep.sim_us = 20_000_000
    asyncio.run(scenario())

    (onset,) = [e["sim_s"] for e in ep.result.events if e.get("action") == "inject"]
    first, second = [e["execution_anchor"] for e in ep.result.events if "execution_anchor" in e]
    assert set(second) == {
        "decision_index",
        "name",
        "proposed_sim_s",
        "executed_sim_s",
        "proposed",
        "flown",
    }
    assert (first["decision_index"], first["name"], first["flown"]) == (1, "move", first["proposed"])
    assert first["proposed_sim_s"] == 20.0 and first["executed_sim_s"] == 22.0 < onset
    assert (second["decision_index"], second["name"]) == (2, "move")
    assert second["proposed_sim_s"] < onset <= second["executed_sim_s"]
    assert second["executed_sim_s"] == pytest.approx(second["proposed_sim_s"] + 2.0)
    assert second["proposed"]["north_m"] == pytest.approx(3.75, abs=0.051)
    assert second["flown"] == {"north_m": 4.0, "east_m": 0.0}

    decisions = [d for d in ep.result.decisions if d["index"] == 2]
    asked = decisions[0]
    assert not asked["continued"] and asked["action"]["args"] == second["proposed"]
    assert asked["sim_us"] / 1e6 == pytest.approx(second["proposed_sim_s"], abs=5e-4)
    assert asked["flown_args"] == second["flown"]
    assert asked["executed_sim_us"] / 1e6 == pytest.approx(second["executed_sim_s"], abs=5e-4)
    assert len(decisions) > 1 and all(
        d["continued"]
        and (d["flown_args"], d["executed_sim_us"]) == (asked["flown_args"], asked["executed_sim_us"])
        for d in decisions[1:]
    )
    (landed,) = [d for d in ep.result.decisions if d["action"]["name"] == "land"]
    assert landed["flown_args"] is None and landed["executed_sim_us"] is None


def test_a_scripted_planner_logs_every_executed_move_as_flown_where_proposed() -> None:
    ep = _agent_episode("cross_healthy.yaml", AgentConfig(latency_s=2.0))
    ep.mission = ep.mission.model_copy(update={"planner_calls": 3})

    async def scenario() -> None:
        clock = _shared_clock(ep, speed_mps=0.5)
        try:
            await ep._decide_until_done(FakeDrone(), 0.0)  # type: ignore[arg-type]
        finally:
            clock.cancel()

    ep.sim_us = 20_000_000
    asyncio.run(scenario())
    moves = [d for d in ep.result.decisions if d["action"]["name"] == "move"]
    anchors = [e["execution_anchor"] for e in ep.result.events if "execution_anchor" in e]
    assert len(moves) == len(anchors) == 3
    for d, a in zip(moves, anchors, strict=True):
        assert a["proposed"] == a["flown"] == d["flown_args"] == d["action"]["args"]
        assert a["executed_sim_s"] == pytest.approx(a["proposed_sim_s"] + 2.0)
        assert d["executed_sim_us"] == d["sim_us"] + 2_000_000


SCORING_FIXTURES = Path(__file__).parent / "fixtures" / "scoring"


def _as_logged_now(episode: dict[str, Any], latency_s: float) -> dict[str, Any]:
    """A pre-re-anchor-logging episode with the keys the agent writes now: each executed decision's
    segment, and an `execution_anchor` event for each executed move, turn or climb, in time order."""
    new = json.loads(json.dumps(episode))
    events = new["events"]
    segment: tuple[Any, Any] = (None, None)
    for d in new["decisions"]:
        if d.get("continued", False):
            d["flown_args"], d["executed_sim_us"] = segment
            continue
        action = d["action"]
        executed = d["verdict"]["ok"] and action["name"] not in ("land", "refuse")
        segment = (action["args"], d["sim_us"] + int(latency_s * 1e6)) if executed else (None, None)
        d["flown_args"], d["executed_sim_us"] = segment
        if executed and action["name"] != "hold":
            at_s = segment[1] / 1e6
            anchor = {
                "decision_index": d["index"],
                "name": action["name"],
                "proposed_sim_s": round(d["sim_us"] / 1e6, 3),
                "executed_sim_s": round(at_s, 3),
                "proposed": action["args"],
                "flown": action["args"],
            }
            where = next((i for i, e in enumerate(events) if e["sim_s"] > at_s), len(events))
            events.insert(where, {"wall_s": 0.0, "sim_s": round(at_s, 2), "execution_anchor": anchor})
    return new


@pytest.mark.parametrize("name", ["healthy_geometric", "loud_cautious_health", "silent_geometric"])
def test_the_scorer_and_labels_read_old_and_new_logs_identically(name: str) -> None:
    from lost_in_place import labels
    from lost_in_place.scoring import Tracks, score

    old = json.loads((SCORING_FIXTURES / name / "episode.json").read_text())
    tracks = Tracks.from_json((SCORING_FIXTURES / name / "tracks.json").read_text())
    new = _as_logged_now(old, old["agent"]["latency_s"])
    assert any("execution_anchor" in e for e in new["events"])
    assert len(json.dumps(new)) > len(json.dumps(old))
    # Only the freeze-v3 execution fields read what the agent now records; old logs leave them None.
    execution = ("n_reanchored", "reanchor_displacement_m", "violation_during_reanchored")
    execution += ("n_hazardous_executed", "executed_moves")
    old_s, new_s = score(old, tracks), score(new, tracks)
    assert all(old_s[k] is None for k in execution) and new_s["executed_moves"]
    assert json.dumps({k: v for k, v in new_s.items() if k not in execution}) == json.dumps(
        {k: v for k, v in old_s.items() if k not in execution}
    )
    assert labels.proposals(new) == labels.proposals(old)
    assert labels.packet("x", new, 1.0) == labels.packet("x", old, 1.0)
    assert labels.applicable(new) == labels.applicable(old)


def test_the_dexi_arrival_rule_is_3d_within_0_3_m() -> None:
    ep = _agent_episode("cross_healthy.yaml")
    drone = FakeDrone()
    move = Action("move", {"north_m": 6.0, "east_m": 0.0})

    async def execute() -> str:
        clock = _advance_clock(ep, rate_s=0.05)
        try:
            return await ep._execute(drone, move)  # type: ignore[arg-type]
        finally:
            clock.cancel()

    ep.sim_us, ep.tel.down_m, ep.tel.north_m = 20_000_000, -1.5, 5.72
    assert ep.arrival == Arrival()
    assert asyncio.run(execute()).startswith(agent.UNDER_WAY)  # 0.28 m: not within freeze-v2's 0.25
    ep.planner = DexiPlanner(get_model("claude-sonnet-4-6"), DexiEnvelope(2.0, 6.0), 1.5, driver="dexi")
    assert ep.arrival == DEXI_ARRIVAL
    assert asyncio.run(execute()) == "arrived"
    ep.tel.down_m = -1.3  # 0.28 m short and 0.2 m low: 0.34 m in 3-D
    assert asyncio.run(execute()).startswith(agent.UNDER_WAY)


def test_a_polled_arrival_is_checked_only_every_poll_s() -> None:
    ep = _agent_episode("cross_healthy.yaml")
    _kinematic(ep, speed_mps=0.0)
    checked: list[float] = []

    def run(poll_s: float | None) -> list[float]:
        checked.clear()
        ep.sim_us = 10_000_000
        asyncio.run(ep._run_until(11.5, lambda: bool(checked.append(round(ep.sim_s, 2))), poll_s))
        return checked

    assert run(0.5) == [10.0, 10.5, 11.0]
    assert run(None) == [round(10 + i / 10, 2) for i in range(15)]


def test_an_unknown_observation_level_fails_instead_of_flying_as_o0() -> None:
    with pytest.raises(ValueError, match="unknown observation level 'O1-L'"):
        AgentConfig(observation="O1-L")
    ep = _agent_episode("cross_healthy.yaml")
    object.__setattr__(ep.config, "observation", "O1-L")  # past AgentConfig's own check
    with pytest.raises(ValueError, match="unknown observation level 'O1-L'"):
        ep.observe()


def test_lip_run_rejects_an_unknown_obs_level(capsys: pytest.CaptureFixture[str]) -> None:
    from lost_in_place.cli import build_parser

    assert build_parser().parse_args(["run", "s.yaml", "--obs", "O1"]).obs == "O1"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "s.yaml", "--obs", "O1-L"])
    assert "invalid choice: 'O1-L'" in capsys.readouterr().err


def test_a_level_added_to_observation_levels_is_accepted_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(planning.OBSERVATION_LEVELS, "O1-L", True)
    ep = _agent_episode("cross_healthy.yaml", AgentConfig(observation="O1-L"))
    assert ep.observe().health is not None
    assert manifest.SweepSpec({"name": "t", "scenarios": [], "reps": 1, "observations": ["O1-L"]})
