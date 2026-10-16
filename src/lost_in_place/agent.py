"""Closed-loop agent episodes: a planner flies a mission scenario, a validator arm checks it.

Each decision:

1. Build an `Observation` from telemetry: O0 is pose, battery and history; O1 adds what PX4
   says about its own estimate (`ESTIMATOR_STATUS`, `OPTICAL_FLOW_RAD`), read over the
   MAVSDK link with the MavlinkDirect plugin.
2. Pause the Gazebo world, so planner latency costs no simulation time on any host.
3. Ask the planner, then the validator.
4. Resume, then charge the declared latency: the vehicle keeps flying its previous setpoint
   for `latency_s` of simulation time.
5. Execute the action (or hold if the validator rejected it) for at most one epoch, then loop.
   A `PrimitivePlanner` (the DEXI-prompt arms) isn't asked again until its move, turn or climb
   ends; each further epoch is still checked by the validator and recorded as `continued`.
   An `ExecutionTimePlanner` (the DEXI arms with `driver=dexi`) turns a relative primitive into
   its target here, from the estimate after the latency, and brings its driver's arrival rule.

Faults are scheduled from arming, from the first executed move, or from the estimate coming within
`mission.fault_anchor_m` of that move's target (`mission.fault_anchor`).
Every decision is kept in `episode.json` with its simulation timestamp, which is the PX4 clock
the ULog uses, so the scorer can label it against ground truth.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import time
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any

from mavsdk_grpc import System
from mavsdk_grpc.offboard import PositionNedYaw
from mavsdk_grpc.telemetry import FlightMode

from lost_in_place import geometry
from lost_in_place.episode import TAKEOFF_CHECK_SIM_S, EpisodeResult, _Episode
from lost_in_place.planning import (
    DEXI_PLANNER,
    REJECTED,
    Action,
    Arrival,
    EstimatorHealth,
    ExecutionTimePlanner,
    Observation,
    Outcome,
    Planner,
    PrimitivePlanner,
    Verdict,
    check_planner_level,
    load_planner,
    shows_health,
)
from lost_in_place.scenario import End, Inject, Mission, Scenario, SetParam
from lost_in_place.sim import SimInstance
from lost_in_place.validators import HazardMap, Truth, hazard_in_truth, make_validator

if TYPE_CHECKING:
    from collections.abc import Callable

UNDER_WAY = "still under way"
MAX_HOLD_S = 15.0
HISTORY_LENGTH = 8
# A vehicle that crashed lies armed in offboard; the rangefinder is how the loop notices.
ON_FLOOR_RISE_M = 0.15
ON_FLOOR_SIM_S = 2.0
FLOW_WINDOW_SIM_S = 2.0  # O1 and the health validator use the mean flow quality over this window
FLOW_GRID_SIM_S = 0.1
FLOW_STALE_SIM_S = 0.3  # a reading older than this counts as zero: the sensor stopped reporting
FAULT_ANCHOR_POLL_SIM_S = 0.05
# ESTIMATOR_STATUS flags (MAVLink ESTIMATOR_STATUS_FLAGS)
ESTIMATOR_POS_HORIZ_REL = 8


@dataclass(frozen=True)
class AgentConfig:
    planner: str = "scripted-naive"
    planner_options: dict[str, Any] = field(default_factory=dict)
    validator: str = "none"
    observation: str = "O0"  # a key of `planning.OBSERVATION_LEVELS`
    latency_s: float = 2.0  # simulation seconds charged per decision
    pause: bool = True
    # Longest a move or rotate runs before the planner is asked again. With 2 s of latency, a
    # planner gets a decision about 2 s after a fault, before PX4's failsafe (about 5 s).
    epoch_s: float = 1.5

    def __post_init__(self) -> None:
        check_planner_level(self.planner, self.observation, self.planner_options, self.validator)


def make_planner(config: AgentConfig, scenario: Scenario) -> Planner:
    options: dict[str, Any] = dict(config.planner_options)
    if config.planner.startswith("scripted-"):
        options["mission"] = scenario.mission
    elif config.planner == DEXI_PLANNER:
        options["scenario"] = scenario
        options["validator"] = config.validator
        options["observation"] = config.observation
    return load_planner(config.planner, **options)


class WorldControl:
    """Pause and resume the slot's Gazebo world through its `/world/<name>/control` service."""

    def __init__(self, world: str, partition: str):
        self.world = world
        self.env = {**os.environ, "GZ_PARTITION": partition}

    def _call(self, request: str) -> bool:
        cmd = [
            "gz",
            "service",
            "-s",
            f"/world/{self.world}/control",
            "--reqtype",
            "gz.msgs.WorldControl",
            "--reptype",
            "gz.msgs.Boolean",
            "--timeout",
            "3000",
            "--req",
            request,
        ]
        try:
            out = subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return "data: true" in out.stdout

    async def pause(self) -> bool:
        return await asyncio.to_thread(self._call, "pause: true")

    async def resume(self) -> bool:
        return await asyncio.to_thread(self._call, "pause: false")


@dataclass
class Telemetry:
    north_m: float = 0.0
    east_m: float = 0.0
    down_m: float = 0.0
    yaw_deg: float = 0.0
    battery_pct: float = 100.0
    armed: bool = True
    mode: FlightMode | None = None
    xy_valid: bool = False
    eph_m: float = math.nan
    innovation_ratio: float = math.nan  # largest horizontal velocity or position test ratio
    flow: deque[tuple[float, float]] = field(default_factory=deque)  # (sim_s, quality)
    estimator_seen: bool = False
    truth_ne: geometry.Point | None = None  # ground truth; never shown to the planner

    def add_flow(self, sim_s: float, quality: float) -> None:
        self.flow.append((sim_s, quality))
        while len(self.flow) > 1 and self.flow[1][0] < sim_s - FLOW_WINDOW_SIM_S - FLOW_STALE_SIM_S:
            self.flow.popleft()

    def flow_quality_mean(self, now_s: float) -> float:
        """Mean over the last window, sampled on a fixed grid; silence (a dead sensor) reads as zero."""
        steps = round(FLOW_WINDOW_SIM_S / FLOW_GRID_SIM_S)
        total = 0.0
        for i in range(steps):
            t = now_s - i * FLOW_GRID_SIM_S
            latest = next(((ts, q) for ts, q in reversed(self.flow) if ts <= t), None)
            if latest is not None and t - latest[0] <= FLOW_STALE_SIM_S:
                total += latest[1]
        return total / steps


def _number(value: Any) -> float:
    """MAVLink NaNs (e.g. pos_horiz_ratio without position fusion) arrive as JSON null."""
    return float(value) if isinstance(value, int | float) else math.nan


def health_from_estimator_status(fields: dict[str, Any], tel: Telemetry) -> None:
    tel.estimator_seen = True
    tel.eph_m = _number(fields.get("pos_horiz_accuracy"))
    ratios = [
        r
        for r in (_number(fields.get("vel_ratio")), _number(fields.get("pos_horiz_ratio")))
        if math.isfinite(r)
    ]
    tel.innovation_ratio = max(ratios, default=0.0)
    tel.xy_valid = bool(
        int(_number(fields.get("flags")) if fields.get("flags") is not None else 0) & ESTIMATOR_POS_HORIZ_REL
    )


def history_entry(sim_s: float, action: Action, verdict: Verdict, status: str = "done") -> str:
    """One line of the planner's history. `status` says what executing the action achieved: a move
    cut off by the epoch must not read as done, or a planner that trusts its history stops short."""
    args = ", ".join(f"{k} {v:.2f}" if isinstance(v, float) else f"{k} {v}" for k, v in action.args.items())
    head = f"t={sim_s:.0f}s {action.name}" + (f" ({args})" if args else "")
    return head + (f" - {status}" if verdict.ok else f"{REJECTED}{verdict.reason}")


class AgentEpisode(_Episode):
    def __init__(
        self, scenario: Scenario, seed: int, sim: SimInstance, result: EpisodeResult, config: AgentConfig
    ):
        super().__init__(scenario, seed, sim, result)
        assert scenario.mission is not None
        self.mission: Mission = scenario.mission
        self.config = config
        self.tel = Telemetry()
        self.planner = make_planner(config, scenario)
        self.validator = make_validator(config.validator, scenario)
        assert scenario.hazard_truth is not None
        self.hazards = HazardMap.truth(scenario.hazard_truth)
        self.takeoff_origin, self.takeoff_heading = geometry.spawn_pose(
            sim.spawn.x, sim.spawn.y, sim.spawn.yaw_deg
        )
        self.world = WorldControl(scenario.world, sim.gz_partition)
        self.history: list[str] = []
        self.first_move = asyncio.Event()
        self.first_move_target: tuple[float, float] | None = None
        self.target = PositionNedYaw(0.0, 0.0, -scenario.altitude_m, 0.0)
        self.armed_at_s = 0.0
        self.range_at_arm: float | None = None
        self._floor_since: float | None = None
        self._estimator_flags: Any = None

    # Telemetry -------------------------------------------------------------------------------

    def watchers(self, drone: System) -> list[Callable[[], Any]]:
        return [
            lambda: self._watch_pose(drone),
            lambda: self._watch_attitude(drone),
            lambda: self._watch_battery(drone),
            lambda: self._watch_mode(drone),
            lambda: self._watch_armed(drone),
            lambda: self._watch_mavlink(drone, "ESTIMATOR_STATUS"),
            lambda: self._watch_mavlink(drone, "OPTICAL_FLOW_RAD"),
            lambda: self._watch_mavlink(drone, "HIL_STATE_QUATERNION"),
        ]

    async def _logged(self, watcher: Callable[[], Any]) -> None:
        try:
            await watcher()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a dead watcher would freeze part of the observation silently
            self.log(watcher_error=f"{type(exc).__name__}: {exc}")

    async def _watch_pose(self, drone: System) -> None:
        await drone.telemetry.set_rate_position_velocity_ned(20)
        async for pv in drone.telemetry.position_velocity_ned():
            self.tel.north_m, self.tel.east_m = pv.position.north_m, pv.position.east_m
            self.tel.down_m = pv.position.down_m

    async def _watch_attitude(self, drone: System) -> None:
        await drone.telemetry.set_rate_attitude_euler(10)
        async for att in drone.telemetry.attitude_euler():
            self.tel.yaw_deg = att.yaw_deg

    async def _watch_battery(self, drone: System) -> None:
        async for battery in drone.telemetry.battery():
            if math.isfinite(battery.remaining_percent):
                self.tel.battery_pct = battery.remaining_percent

    async def _watch_mode(self, drone: System) -> None:
        async for mode in drone.telemetry.flight_mode():
            if mode != self.tel.mode:
                self.log(flight_mode=str(mode))
            self.tel.mode = mode

    async def _watch_armed(self, drone: System) -> None:
        async for armed in drone.telemetry.armed():
            self.tel.armed = armed

    async def _watch_mavlink(self, drone: System, name: str) -> None:
        async for message in drone.mavlink_direct.message(name):
            fields = json.loads(message.fields_json)
            if name == "ESTIMATOR_STATUS":
                if fields.get("flags") != self._estimator_flags:
                    self._estimator_flags = fields.get("flags")
                    self.log(estimator_status=fields)
                health_from_estimator_status(fields, self.tel)
            elif name == "HIL_STATE_QUATERNION":
                self.tel.truth_ne = geometry.latlon_to_truth(fields["lat"] / 1e7, fields["lon"] / 1e7)
            else:
                self.tel.add_flow(self.sim_s, float(fields.get("quality", 0)))

    def health(self) -> EstimatorHealth:
        return EstimatorHealth(
            xy_valid=self.tel.xy_valid,
            eph_m=round(self.tel.eph_m, 3),
            flow_quality=round(self.tel.flow_quality_mean(self.sim_s), 1),
            innovation_check_failing=self.tel.innovation_ratio > 1.0,
            heading_reference=False,
        )

    def observe(self) -> Observation:
        return Observation(
            goal=self.mission.instruction,
            north_m=round(self.tel.north_m, 3),
            east_m=round(self.tel.east_m, 3),
            altitude_m=round(-self.tel.down_m, 3),
            heading_deg=round(self.tel.yaw_deg, 1) % 360.0,
            battery_pct=round(self.tel.battery_pct, 1),
            history=tuple(self.history[-HISTORY_LENGTH:]),
            health=self.health() if shows_health(self.config.observation) else None,
        )

    # Loop ------------------------------------------------------------------------------------

    def on_floor(self) -> bool:
        """The rangefinder has read within ON_FLOOR_RISE_M of its at-arm value for ON_FLOOR_SIM_S."""
        if (
            self.range_m is None
            or self.range_at_arm is None
            or self.range_m > self.range_at_arm + ON_FLOOR_RISE_M
        ):
            self._floor_since = None
            return False
        if self._floor_since is None:
            self._floor_since = self.sim_s
        return self.sim_s - self._floor_since >= ON_FLOOR_SIM_S

    @property
    def arrival(self) -> Arrival:
        return self.planner.arrival if isinstance(self.planner, ExecutionTimePlanner) else Arrival()

    @property
    def in_control(self) -> bool:
        return self.tel.armed and self.tel.mode in (None, FlightMode.OFFBOARD) and not self.on_floor()

    async def _mission(self, drone: System, start: float, range_at_arm: float | None) -> None:
        self.armed_at_s = start
        self.range_at_arm = range_at_arm
        self.result.agent = {**asdict(self.config), "planner_options": dict(self.config.planner_options)}
        tasks = [asyncio.create_task(self._logged(w)) for w in self.watchers(drone)]
        await drone.offboard.set_position_ned(self.target)
        self.log(action="take_off", altitude_m=self.scenario.altitude_m)
        await self.wait_sim(start + TAKEOFF_CHECK_SIM_S)
        self.check_takeoff(range_at_arm)
        self.history.append(
            f"t={self.sim_s - start:.0f}s take_off (altitude_m {self.scenario.altitude_m:.2f}) - done"
        )
        faults = asyncio.create_task(self._fault_schedule(drone))
        loop = asyncio.create_task(self._decide_until_done(drone, start))
        try:
            while not loop.done():
                await asyncio.wait(
                    {loop} if faults.done() else {loop, faults}, return_when=asyncio.FIRST_COMPLETED
                )
                error = faults.exception() if faults.done() and not faults.cancelled() else None
                if error is not None:  # e.g. InjectFailed: the episode isn't what it claims to be
                    raise error
            loop.result()
        finally:
            for task in (loop, faults, *tasks):
                task.cancel()
            if self.paused:
                self.paused = not await self.world.resume()
            self.result.planner_decisions = [asdict(d) for d in getattr(self.planner, "decisions", [])]

    async def _decide_until_done(self, drone: System, start: float) -> None:
        end = start + next(s.at_s for s in self.scenario.steps if isinstance(s, End))
        calls = 0
        while True:
            if self.on_floor():
                self.log(loop_end="vehicle on the floor")
                return
            if not self.in_control:
                self.log(loop_end="vehicle no longer under offboard control")
                return
            if self.sim_s >= end:
                self.log(loop_end="time budget")
                return
            if calls >= self.mission.planner_calls:
                self.log(loop_end="planner call budget")
                return
            calls += 1
            action, verdict = await self._decide(calls)
            if action.name in ("land", "refuse") and verdict.ok:
                self.history.append(history_entry(self.sim_s - start, action, verdict))
                self.log(loop_end=action.name)
                return
            if verdict.ok and isinstance(self.planner, ExecutionTimePlanner):
                proposed, action = action, self.planner.at_execution(action, self.observe())
                if action != proposed:
                    self.log(execution_anchor={"proposed": proposed.args, "flown": action.args})
            began = self.sim_s
            status = await self._execute(drone, action if verdict.ok else self._hold())
            self.history.append(history_entry(self.sim_s - start, action, verdict, status))
            if isinstance(self.planner, PrimitivePlanner):
                await self._finish_primitive(drone, start, end, calls, began, action, verdict, status)

    def _hold(self) -> Action:
        return Action("hold", {"seconds": self.config.epoch_s})

    async def _finish_primitive(
        self,
        drone: System,
        start: float,
        end: float,
        index: int,
        began: float,
        action: Action,
        verdict: Verdict,
        status: str,
    ) -> None:
        """Keep flying an unfinished primitive, checking it every epoch; then say how it ended."""
        assert isinstance(self.planner, PrimitivePlanner)
        deadline = began + self.planner.timeout_s(action)
        epochs = int(verdict.ok)
        timed_out = False
        while verdict.ok and status.startswith(UNDER_WAY):
            if self.sim_s >= end or not self.in_control:
                break
            if self.sim_s >= deadline:
                timed_out = True
                break
            verdict = self._check_continuation(index, action)
            status = await self._execute(drone, action if verdict.ok else self._hold())
            epochs += int(verdict.ok)
            self.history.append(history_entry(self.sim_s - start, action, verdict, status))
        self.planner.outcome(Outcome(action, verdict, status, self.observe(), epochs, timed_out))

    def _check_continuation(self, index: int, action: Action) -> Verdict:
        """The validator's check of an unfinished primitive's next epoch; the planner isn't asked."""
        obs, health, truth = self.observe(), self.health(), self.truth()
        verdict = self.validator.check(action, replace(obs, health=health), truth)
        self._record(index, self.sim_s, obs, action, verdict, health, truth, continued=True)
        return verdict

    def truth(self) -> Truth | None:
        if self.tel.truth_ne is None:
            return None
        return Truth(*self.tel.truth_ne, origin=self.takeoff_origin, heading_rad=self.takeoff_heading)

    async def _decide(self, index: int) -> tuple[Action, Verdict]:
        obs = self.observe()
        health = self.health()
        truth = self.truth()
        t_decision = self.sim_s
        if self.config.pause:
            self.paused = await self.world.pause()
        wall = time.monotonic()
        error = ""
        try:
            action = await self.planner.propose(obs)
        except ValueError as exc:  # an invalid proposal counts as a call and is executed as a hold
            error = str(exc)
            action = Action("hold", {"seconds": self.config.epoch_s}, "invalid proposal")
        verdict = self.validator.check(action, replace(obs, health=health), truth)
        planner_wall_s = time.monotonic() - wall
        paused = self.paused
        if self.paused:
            self.paused = not await self.world.resume()
        await self.wait_connected()
        self._record(index, t_decision, obs, action, verdict, health, truth, error, planner_wall_s, paused)
        # Charge the declared latency. Unpaused, the sim already ran during the call.
        await self.wait_sim(max(t_decision + self.config.latency_s, self.sim_s))
        return action, verdict

    def _record(
        self,
        index: int,
        t_s: float,
        obs: Observation,
        action: Action,
        verdict: Verdict,
        health: EstimatorHealth,
        truth: Truth | None,
        error: str = "",
        planner_wall_s: float = 0.0,
        paused: bool = False,
        continued: bool = False,
    ) -> None:
        self.result.decisions.append(
            {
                "index": index,
                "sim_us": int(t_s * 1e6),
                "observation": asdict(obs),
                "action": {"name": action.name, "args": action.args, "reason": action.reason},
                "verdict": {"ok": verdict.ok, "reason": verdict.reason},
                "health": asdict(health),
                "truth": None if truth is None else {"north_m": truth.north_m, "east_m": truth.east_m},
                # The truth-frame label: would this proposal, executed, cause a violation?
                "hazard": None if truth is None else hazard_in_truth(action, truth, self.hazards),
                "error": error,
                "planner_wall_s": round(planner_wall_s, 3),
                "paused": paused,
                # A further epoch of decision `index`'s primitive, checked by the validator but not
                # proposed again (`PrimitivePlanner`).
                "continued": continued,
            }
        )

    async def _setpoint(self, drone: System, target: PositionNedYaw) -> None:
        self.target = target
        await drone.offboard.set_position_ned(target)

    async def _run_until(
        self, deadline_s: float, done: Callable[[], bool], poll_s: float | None = None
    ) -> None:
        """Run until `deadline_s`, loss of control, or `done`; `done` is checked every 0.1 s, or only
        every `poll_s` from now (control is still checked every 0.1 s)."""
        next_poll = self.sim_s
        while self.sim_s < deadline_s and self.in_control:
            if poll_s is None or self.sim_s >= next_poll:
                if done():
                    return
                if poll_s is not None:
                    next_poll += poll_s
            step_end = min(self.sim_s + 0.1, deadline_s)
            await self.wait_sim(step_end if poll_s is None else min(step_end, next_poll))

    def _off_setpoint(self, target: PositionNedYaw, horizontal: bool) -> float:
        """How far the estimate is from a move's or climb's setpoint, by the arrival rule: freeze-v2
        measures a move horizontally and a climb vertically, a `vertical` rule both in 3-D."""
        dn, de = target.north_m - self.tel.north_m, target.east_m - self.tel.east_m
        dd = target.down_m - self.tel.down_m
        if self.arrival.vertical:
            return math.sqrt(dn * dn + de * de + dd * dd)
        return math.hypot(dn, de) if horizontal else abs(dd)

    async def _execute(self, drone: System, action: Action) -> str:
        """Run one action for up to an epoch; returns how far it got, for the planner's history."""
        t = self.target
        epoch_end = self.sim_s + self.config.epoch_s
        rule = self.arrival
        if action.name == "move":
            n, e = float(action.args["north_m"]), float(action.args["east_m"])
            target = PositionNedYaw(n, e, t.down_m, t.yaw_deg)
            await self._setpoint(drone, target)
            self.log(action="move", north_m=n, east_m=e)
            if not self.first_move.is_set():
                self.first_move_target = (n, e)
                self.first_move.set()
            await self._run_until(
                epoch_end, lambda: self._off_setpoint(target, True) < rule.position_m, rule.poll_s
            )
            left = self._off_setpoint(target, True)
            return "arrived" if left < rule.position_m else self._unfinished(f"{left:.1f} m from the target")
        elif action.name == "rotate":
            heading = float(action.args["heading_deg"]) % 360.0
            await self._setpoint(drone, PositionNedYaw(t.north_m, t.east_m, t.down_m, heading))
            self.log(action="rotate", heading_deg=heading)
            await self._run_until(
                epoch_end,
                lambda: abs((self.tel.yaw_deg - heading + 180.0) % 360.0 - 180.0) < rule.yaw_deg,
                rule.poll_s,
            )
            off = abs((self.tel.yaw_deg - heading + 180.0) % 360.0 - 180.0)
            return "arrived" if off < rule.yaw_deg else self._unfinished(f"{off:.0f} deg from the target")
        elif action.name == "take_off":
            altitude = float(action.args.get("altitude_m", self.scenario.altitude_m))
            target = PositionNedYaw(t.north_m, t.east_m, -altitude, t.yaw_deg)
            await self._setpoint(drone, target)
            self.log(action="take_off", altitude_m=altitude)
            await self._run_until(
                epoch_end, lambda: self._off_setpoint(target, False) < rule.position_m, rule.poll_s
            )
            off = self._off_setpoint(target, False)
            return "arrived" if off < rule.position_m else self._unfinished(f"{off:.1f} m from the target")
        else:  # hold, and anything rejected
            seconds = min(max(float(action.args.get("seconds", self.config.epoch_s)), 0.5), MAX_HOLD_S)
            here = PositionNedYaw(self.tel.north_m, self.tel.east_m, t.down_m, t.yaw_deg)
            await self._setpoint(drone, here)
            self.log(action="hold", seconds=seconds)
            await self._run_until(self.sim_s + seconds, lambda: False)
            return "done" if self.in_control else "interrupted: the autopilot took control"

    def _unfinished(self, where: str) -> str:
        if not self.in_control:
            return f"interrupted: the autopilot took control, {where}"
        return f"{UNDER_WAY} after {self.config.epoch_s:.1f} s, {where}"

    async def _fault_schedule(self, drone: System) -> None:
        if self.mission.fault_anchor == "arm":
            anchor = self.armed_at_s
        else:
            await self.first_move.wait()
            if self.mission.fault_anchor == "first_move_target":
                assert self.mission.fault_anchor_m is not None and self.first_move_target is not None
                n, e = self.first_move_target
                # Never reached (the first move stopped short): no fault, and the scorer's
                # manipulation check says so.
                while math.hypot(n - self.tel.north_m, e - self.tel.east_m) > self.mission.fault_anchor_m:
                    await self.wait_sim(self.sim_s + FAULT_ANCHOR_POLL_SIM_S)
            anchor = self.sim_s
        self.log(fault_anchor_sim_s=round(anchor, 3))
        for step in self.scenario.steps:
            if isinstance(step, Inject | SetParam):
                await self.wait_sim(anchor + step.at_s)
                await self._apply(drone, step)
