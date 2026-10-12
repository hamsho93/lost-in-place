"""Run one scenario end to end and write `episode.json` plus the PX4 ULog.

All mission timing follows simulation time (PX4 runs in lockstep with Gazebo, usually
slower than real time), read from the HIGHRES_IMU timestamp.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import shutil
import time
import traceback
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from mavsdk_grpc import System
from mavsdk_grpc.failure import FailureType, FailureUnit
from mavsdk_grpc.offboard import OffboardError, PositionNedYaw

from lost_in_place import provenance
from lost_in_place.config import FLOW_ENABLE_AFTER_SIM_S, HARNESS_PARAMS, VEHICLE_PARAMS, Paths
from lost_in_place.scenario import End, Goto, Inject, Scenario, SetParam
from lost_in_place.sim import SimInstance, offboard_port

BOOT_STALL_WALL_S = 150.0  # sim clock still at zero this long after connect: a wedged boot
SIM_STALL_WALL_S = 90.0  # sim clock frozen mid-episode: Gazebo died (e.g. after a runaway)
LOCAL_POSITION_TIMEOUT_WALL_S = 120.0
ARM_TIMEOUT_WALL_S = 90.0
LAND_TIMEOUT_WALL_S = 90.0
EPISODE_TIMEOUT_WALL_S = 900.0
MAX_BOOT_ATTEMPTS = 3
# Good runs are 0.4 m or more above the floor this long after arming; every scenario's first
# fault comes later, so a stuck vehicle is caught before it can be mistaken for a fault response.
TAKEOFF_CHECK_SIM_S = 8.0
TAKEOFF_MIN_RISE_M = 0.3
RETRIED_OUTCOMES = ("boot_stall", "stuck_on_ground")


class BootStall(RuntimeError):
    pass


class SimStall(RuntimeError):
    pass


class StuckOnGround(RuntimeError):
    """PX4 commands full thrust but Gazebo's vehicle doesn't move (seen on a packed, fresh host)."""


class InjectFailed(RuntimeError):
    """PX4 refused a scheduled fault, so the episode would be labelled degraded while healthy."""


@dataclass
class EpisodeResult:
    scenario_id: str
    seed: int
    instance: int
    world: str
    spawn: dict[str, float]
    outcome: str = "pending"
    failsafe: bool = False
    boot_attempts: int = 0
    retried: list[str] = field(default_factory=list)  # outcomes of earlier boots of this episode
    real_time_factor: float | None = None  # sim seconds per wall second, from connect to stop
    wall_s: dict[str, float] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    params: dict[str, int | float] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)


_FAILURE_UNITS = {
    "optical_flow": "SENSOR_OPTICAL_FLOW",
    "distance_sensor": "SENSOR_DISTANCE_SENSOR",
    "gyro": "SENSOR_GYRO",
    "accel": "SENSOR_ACCEL",
    "baro": "SENSOR_BARO",
    "battery": "SYSTEM_BATTERY",
}


def failure_enums(step: Inject) -> tuple[Any, Any]:
    """Map a scenario fault onto MAVSDK's failure unit and type."""
    return getattr(FailureUnit, _FAILURE_UNITS[step.unit]), getattr(FailureType, step.type.upper())


class _Episode:
    def __init__(self, scenario: Scenario, seed: int, sim: SimInstance, result: EpisodeResult):
        self.scenario = scenario
        self.seed = seed
        self.sim = sim
        self.result = result
        self.sim_us = 0
        self.range_m: float | None = None
        self._t0 = time.monotonic()
        self._clock_start: tuple[float, int] | None = None  # (wall, sim_us) at the first sim tick

    def real_time_factor(self) -> float | None:
        if self._clock_start is None:
            return None
        wall = time.monotonic() - self._clock_start[0]
        sim = (self.sim_us - self._clock_start[1]) / 1e6
        return round(sim / wall, 3) if wall > 0 and sim > 0 else None

    def log(self, **event: Any) -> None:
        self.result.events.append(
            {"wall_s": round(time.monotonic() - self._t0, 2), "sim_s": round(self.sim_s, 2), **event}
        )

    @property
    def sim_s(self) -> float:
        return self.sim_us / 1e6

    def mark(self, name: str) -> None:
        self.result.wall_s[name] = round(time.monotonic() - self._t0, 1)

    async def wait_sim(self, target_s: float) -> None:
        last_change, last_value = time.monotonic(), self.sim_us
        while self.sim_s < target_s:
            await asyncio.sleep(0.1)
            if self.sim_us != last_value:
                last_change, last_value = time.monotonic(), self.sim_us
            elif self.sim_us == 0 and time.monotonic() - last_change > BOOT_STALL_WALL_S:
                raise BootStall("simulation clock never started")
            elif self.sim_us > 0 and time.monotonic() - last_change > SIM_STALL_WALL_S:
                raise SimStall(f"simulation clock frozen at {self.sim_s:.1f} s")

    async def run(self) -> str:
        drone = System(port=51000 + 10 * self.sim.instance)
        await drone.connect(system_address=f"udpin://0.0.0.0:{offboard_port(self.sim.instance)}")
        async for state in drone.core.connection_state():
            if state.is_connected:
                break
        self.mark("connected")
        tasks = [
            asyncio.create_task(self._watch_status(drone)),
            asyncio.create_task(self._watch_clock(drone)),
            asyncio.create_task(self._watch_range(drone)),
        ]
        try:
            return await self._fly(drone)
        finally:
            for task in tasks:
                task.cancel()
            server = drone._server_process
            if server is not None and server.poll() is not None:
                self.log(mavsdk_server_exit=server.returncode)
            # The mavsdk_server child otherwise outlives the episode and keeps the process alive.
            drone._stop_mavsdk_server()

    async def _watch_status(self, drone: System) -> None:
        async for text in drone.telemetry.status_text():
            message = f"{text.type}: {text.text}".strip()
            if "failsafe" in message.lower():
                self.result.failsafe = True
            self.log(status=message)

    async def _watch_clock(self, drone: System) -> None:
        await drone.telemetry.set_rate_imu(50)
        async for imu in drone.telemetry.imu():
            self.sim_us = int(imu.timestamp_us)
            if self._clock_start is None and self.sim_us > 0:
                self._clock_start = (time.monotonic(), self.sim_us)

    async def _watch_range(self, drone: System) -> None:
        await drone.telemetry.set_rate_distance_sensor(10)
        async for reading in drone.telemetry.distance_sensor():
            if math.isfinite(reading.current_distance_m):
                self.range_m = reading.current_distance_m

    def check_takeoff(self, range_at_arm: float | None) -> None:
        if range_at_arm is None or self.range_m is None:
            self.log(takeoff_check="no rangefinder data")
            return
        rise = self.range_m - range_at_arm
        self.log(takeoff_check_rise_m=round(rise, 2))
        if rise < TAKEOFF_MIN_RISE_M:
            raise StuckOnGround(f"rangefinder rose {rise:.2f} m in {TAKEOFF_CHECK_SIM_S:.0f} s after arming")

    async def _fly(self, drone: System) -> str:
        await self.wait_sim(FLOW_ENABLE_AFTER_SIM_S)
        await drone.param.set_param_int("EKF2_OF_CTRL", 1)
        self.log(action="enable_flow_fusion")

        deadline = time.monotonic() + LOCAL_POSITION_TIMEOUT_WALL_S
        while not (await anext(drone.telemetry.health())).is_local_position_ok:
            if time.monotonic() > deadline:
                return "no_local_position"
            await asyncio.sleep(0.5)
        self.mark("local_position_ok")

        # Boot mode is Hold, which needs a global position; enter offboard while disarmed, then arm.
        deadline = time.monotonic() + ARM_TIMEOUT_WALL_S
        while True:
            try:
                await drone.offboard.set_position_ned(PositionNedYaw(0.0, 0.0, 0.0, 0.0))
                await drone.offboard.start()
                await drone.action.arm()
                break
            except Exception as exc:  # MAVSDK raises several unrelated types while PX4 is not ready
                if time.monotonic() > deadline:
                    self.log(error=f"arming denied: {exc}")
                    return "arming_denied"
                await asyncio.sleep(2)
        self.mark("armed")

        start = self.sim_s
        range_at_arm = self.range_m
        self.log(action="offboard_start")
        takeoff_checked = False
        for step in self.scenario.steps:
            if not takeoff_checked and step.at_s > TAKEOFF_CHECK_SIM_S:
                await self.wait_sim(start + TAKEOFF_CHECK_SIM_S)
                self.check_takeoff(range_at_arm)
                takeoff_checked = True
            await self.wait_sim(start + step.at_s)
            await self._apply(drone, step)
            if isinstance(step, End):
                break
        self.mark("mission_end")

        with contextlib.suppress(OffboardError):
            await drone.offboard.stop()
        with contextlib.suppress(Exception):  # already landing, or disarmed after a failsafe
            await drone.action.land()
        deadline = time.monotonic() + LAND_TIMEOUT_WALL_S
        while time.monotonic() < deadline and await anext(drone.telemetry.armed()):
            await asyncio.sleep(0.5)
        self.mark("landed")
        return "completed"

    async def _apply(self, drone: System, step: Goto | Inject | SetParam | End) -> None:
        if isinstance(step, Goto):
            await drone.offboard.set_position_ned(
                PositionNedYaw(step.north_m, step.east_m, -self.scenario.altitude_m, 0.0)
            )
            self.log(action="goto", north_m=step.north_m, east_m=step.east_m)
        elif isinstance(step, Inject):
            unit, ftype = failure_enums(step)
            try:
                await drone.failure.inject(unit, ftype, 0)
                ack = "accepted"
            except Exception as exc:  # e.g. UNSUPPORTED on a PX4 build without the flow patch
                ack = str(exc).splitlines()[0]
            self.log(action="inject", unit=step.unit, type=step.type, ack=ack)
            if ack != "accepted":
                raise InjectFailed(f"{step.unit} {step.type}: {ack}")
        elif isinstance(step, SetParam):
            await drone.param.set_param_float(step.name, step.value)
            self.log(action="param", name=step.name, value=step.value)
        else:
            self.log(action="end")


def _error_text(exc: Exception) -> str:
    # gRPC errors render as a multi-line repr; details() is the server's actual message.
    details = getattr(exc, "details", None)
    return str(details() or "") if callable(details) else str(exc)


@contextlib.contextmanager
def _mavsdk_server_log(path: Path) -> Iterator[None]:
    """Write mavsdk_server's output to `path`; MAVSDK otherwise drops everything below warnings."""
    logger = logging.getLogger("mavsdk_server")
    handler = logging.FileHandler(path, mode="w")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
        handler.close()


def run_episode(
    scenario: Scenario, seed: int, out_dir: Path, *, instance: int = 0, paths: Paths | None = None
) -> EpisodeResult:
    """Fly one scenario; reboots the sim after a wedged boot or a vehicle that never left the
    ground, up to MAX_BOOT_ATTEMPTS boots in all."""
    paths = paths or Paths.from_env()
    missing = paths.missing()
    if missing:
        raise FileNotFoundError(f"PX4 build not found: {', '.join(map(str, missing))}")
    out_dir.mkdir(parents=True, exist_ok=True)
    spawn = scenario.spawn_for(seed)
    params = {**VEHICLE_PARAMS, **HARNESS_PARAMS, "EKF2_OF_CTRL": 0}
    result = EpisodeResult(
        scenario_id=scenario.id,
        seed=seed,
        instance=instance,
        world=scenario.world,
        spawn=spawn.model_dump(),
        params=params,
        provenance=provenance.collect(scenario.model_dump_json()),
    )
    sim = SimInstance(paths=paths, world=scenario.world, spawn=spawn, params=params, instance=instance)

    with _mavsdk_server_log(out_dir / "mavsdk_server.txt"):
        for attempt in range(1, MAX_BOOT_ATTEMPTS + 1):
            if attempt > 1:
                result.retried.append(result.outcome)
            result.boot_attempts = attempt
            result.events.clear()
            result.wall_s.clear()
            result.failsafe = False
            episode = _Episode(scenario, seed, sim, result)
            sim.start(out_dir / "px4_console.txt")
            try:
                result.outcome = asyncio.run(asyncio.wait_for(episode.run(), EPISODE_TIMEOUT_WALL_S))
            except BootStall:
                result.outcome = "boot_stall"
            except StuckOnGround as exc:
                result.outcome = f"stuck_on_ground: {exc}"
            except InjectFailed as exc:
                result.outcome = f"inject_failed: {exc}"
            except SimStall as exc:
                result.outcome = f"sim_stall: {exc}"
            except TimeoutError:
                result.outcome = "timeout"
            except Exception as exc:
                # Recorded rather than raised, so episode.json, the console log and the ULog are still
                # written (and uploaded) for a crashed episode.
                traceback.print_exc()
                first_line = next(iter(_error_text(exc).strip().splitlines()), "")
                result.outcome = f"error: {type(exc).__name__}" + (f": {first_line}" if first_line else "")
            finally:
                episode.mark("stopped")
                result.real_time_factor = episode.real_time_factor()
                sim.stop()
            if not result.outcome.startswith(RETRIED_OUTCOMES):
                break

    ulog = sim.latest_ulog()
    if ulog is not None:
        shutil.copy(ulog, out_dir / "flight.ulg")
    (out_dir / "episode.json").write_text(json.dumps(asdict(result), indent=1, default=str))
    return result
