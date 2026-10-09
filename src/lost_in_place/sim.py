"""Start and stop one isolated PX4 SITL + Gazebo instance.

Each instance gets its own Gazebo partition, PX4 instance number (MAVLink offboard port
14540 + instance) and working directory, so several can run on one host. On 4 vCPUs, two
concurrent instances were reliable; three were not.
"""

from __future__ import annotations

import contextlib
import math
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from lost_in_place.config import AIRFRAME, MODEL, Paths
from lost_in_place.scenario import Spawn


def offboard_port(instance: int) -> int:
    return 14540 + instance


@dataclass
class SimInstance:
    paths: Paths
    world: str
    spawn: Spawn
    params: dict[str, int | float]
    instance: int = 0
    workdir: Path = field(default_factory=lambda: Path("/tmp/lost-in-place"))
    _proc: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)

    @property
    def run_dir(self) -> Path:
        return self.workdir / f"instance{self.instance}"

    def _worlds_dir(self) -> Path:
        if (self.paths.worlds_dir / f"{self.world}.sdf").exists():
            return self.paths.worlds_dir
        if (self.paths.gz_worlds / f"{self.world}.sdf").exists():
            return self.paths.gz_worlds
        raise FileNotFoundError(f"world '{self.world}' not found; run `lip worlds` first")

    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("DISPLAY", None)  # headless rendering uses EGL; an X display breaks Ogre2 here
        models = [str(self.paths.gz_models), str(self.paths.worlds_dir / "models")]
        worlds = self._worlds_dir()
        env.update(
            HEADLESS="1",
            PX4_SYS_AUTOSTART=str(AIRFRAME),
            PX4_SIM_MODEL=MODEL,
            PX4_GZ_WORLD=self.world,
            PX4_GZ_NO_FOLLOW="1",
            PX4_GZ_MODEL_POSE=f"{self.spawn.x:.3f},{self.spawn.y:.3f},0.2,0,0,{math.radians(self.spawn.yaw_deg):.4f}",
            PX4_GZ_MODELS=str(self.paths.gz_models),
            PX4_GZ_WORLDS=str(worlds),
            PX4_GZ_PLUGINS=str(self.paths.gz_plugins),
            PX4_GZ_SERVER_CONFIG=str(self.paths.gz_server_config),
            GZ_SIM_RESOURCE_PATH=":".join([*models, str(worlds)]),
            GZ_SIM_SYSTEM_PLUGIN_PATH=str(self.paths.gz_plugins),
            GZ_SIM_SERVER_CONFIG_PATH=str(self.paths.gz_server_config),
            GZ_PARTITION=f"lip{self.instance}",
        )
        env.update({f"PX4_PARAM_{k}": str(v) for k, v in self.params.items()})
        return env

    def start(self, console_log: Path) -> None:
        if self.run_dir.exists():
            shutil.rmtree(self.run_dir)  # params persist in the workdir; every episode starts clean
        self.run_dir.mkdir(parents=True)
        cmd = [
            str(self.paths.px4_bin),
            "-i",
            str(self.instance),
            "-d",
            "-w",
            str(self.run_dir),
            str(self.paths.px4_etc),
        ]
        with console_log.open("wb") as log:
            self._proc = subprocess.Popen(
                cmd,
                cwd=self.run_dir,
                env=self.environment(),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # own process group: Gazebo is started by PX4's rc script
            )

    def stop(self) -> None:
        if self._proc is None:
            return
        for sig in (signal.SIGINT, signal.SIGKILL):
            try:
                os.killpg(self._proc.pid, sig)
            except ProcessLookupError:
                break
            time.sleep(3)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self._proc.wait(timeout=5)
        self._proc = None

    def latest_ulog(self) -> Path | None:
        logs = sorted(self.run_dir.rglob("*.ulg"), key=lambda p: p.stat().st_mtime)
        return logs[-1] if logs else None
