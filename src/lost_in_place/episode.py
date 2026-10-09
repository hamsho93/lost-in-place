"""Run one scenario end to end and write `episode.json` plus the PX4 ULog.

All mission timing follows simulation time (PX4 runs in lockstep with Gazebo, usually
slower than real time), read from the HIGHRES_IMU timestamp.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from mavsdk_grpc import System
from mavsdk_grpc.failure import FailureType, FailureUnit
from mavsdk_grpc.offboard import OffboardError, PositionNedYaw

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


class BootStall(RuntimeError):
    pass


class SimStall(RuntimeError):
    pass


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
    wall_s: dict[str, float] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    params: dict[str, int | float] = field(default_factory=dict)


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
        self._t0 = time.monotonic()

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
        ]
        try:
            return await self._fly(drone)
        finally:
            for task in tasks:
                task.cancel()
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
        self.log(action="offboard_start")
        for step in self.scenario.steps:
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
        elif isinstance(step, SetParam):
            await drone.param.set_param_float(step.name, step.value)
            self.log(action="param", name=step.name, value=step.value)
        else:
            self.log(action="end")


def run_episode(
    scenario: Scenario, seed: int, out_dir: Path, *, instance: int = 0, paths: Paths | None = None
) -> EpisodeResult:
    """Fly one scenario; retries a wedged boot up to MAX_BOOT_ATTEMPTS times."""
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
    )
    sim = SimInstance(paths=paths, world=scenario.world, spawn=spawn, params=params, instance=instance)

    for attempt in range(1, MAX_BOOT_ATTEMPTS + 1):
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
        except SimStall as exc:
            result.outcome = f"sim_stall: {exc}"
        except TimeoutError:
            result.outcome = "timeout"
        finally:
            episode.mark("stopped")
            sim.stop()
        if result.outcome != "boot_stall":
            break

    ulog = sim.latest_ulog()
    if ulog is not None:
        shutil.copy(ulog, out_dir / "flight.ulg")
    (out_dir / "episode.json").write_text(json.dumps(asdict(result), indent=1, default=str))
    return result
