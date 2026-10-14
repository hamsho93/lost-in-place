import asyncio
from pathlib import Path
from typing import Any

import pytest
from mavsdk_grpc.telemetry import FlightMode

from lost_in_place import agent, geometry
from lost_in_place import episode as episode_module
from lost_in_place.agent import AgentConfig, AgentEpisode, Telemetry, health_from_estimator_status
from lost_in_place.config import Paths
from lost_in_place.episode import EpisodeResult, InjectFailed
from lost_in_place.planning import Action, EstimatorHealth, Observation, Outcome, Verdict
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


class FakeDrone:
    def __init__(self, failure_error: str = "") -> None:
        self.offboard = FakeOffboard()
        self.failure = FakeFailure(failure_error)


def _agent_episode(scenario_file: str, config: AgentConfig | None = None) -> AgentEpisode:
    scenario = load_scenario(PILOT / scenario_file)
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
