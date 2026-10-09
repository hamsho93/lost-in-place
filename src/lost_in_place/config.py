"""Filesystem locations and the vehicle configuration shared by every episode."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# DEXI-like estimator setup: no GPS, no magnetometer, EKF2 on optical flow + rangefinder only.
VEHICLE_PARAMS: dict[str, int | float] = {
    "EKF2_MAG_TYPE": 5,
    "SYS_HAS_MAG": 0,
    "EKF2_GPS_CTRL": 0,
    "SYS_HAS_GPS": 0,
    "EKF2_RNG_CTRL": 1,
    "COM_ARM_WO_GPS": 1,
}

# Harness settings: allow failure injection, offboard without RC, log from boot until disarm.
HARNESS_PARAMS: dict[str, int | float] = {
    "SYS_FAILURE_EN": 1,
    "COM_RC_IN_MODE": 4,
    "NAV_DLL_ACT": 0,
    "COM_DL_LOSS_T": 60,
    "SDLOG_MODE": 1,
    "SDLOG_PROFILE": 3,
}

AIRFRAME = 4021
MODEL = "gz_x500_flow"

# If flow fusion starts before fake-position fusion has shrunk the initial position variance,
# eph can stay at sqrt(2) * EKF2_NOAID_NOISE (~14 m) for the whole boot. Enabling flow only
# after the estimator has settled at rest avoids that race (seen in 3 of 6 at-rest boots).
FLOW_ENABLE_AFTER_SIM_S = 12.0


@dataclass(frozen=True)
class Paths:
    """Where PX4 and the generated worlds live. Override with LIP_PX4_DIR / LIP_WORLDS_DIR."""

    px4_dir: Path
    build_dir: Path
    worlds_dir: Path

    @classmethod
    def from_env(cls) -> Paths:
        px4 = Path(os.environ.get("LIP_PX4_DIR", Path.home() / "PX4-Autopilot")).expanduser()
        build = Path(os.environ.get("LIP_PX4_BUILD_DIR", px4 / "build" / "px4_sitl_default")).expanduser()
        worlds = Path(
            os.environ.get("LIP_WORLDS_DIR", Path.home() / ".cache" / "lost-in-place" / "worlds")
        ).expanduser()
        return cls(px4_dir=px4, build_dir=build, worlds_dir=worlds)

    @property
    def px4_bin(self) -> Path:
        return self.build_dir / "bin" / "px4"

    @property
    def px4_etc(self) -> Path:
        return self.build_dir / "etc"

    @property
    def gz_models(self) -> Path:
        return self.px4_dir / "Tools" / "simulation" / "gz" / "models"

    @property
    def gz_worlds(self) -> Path:
        return self.px4_dir / "Tools" / "simulation" / "gz" / "worlds"

    @property
    def gz_plugins(self) -> Path:
        return self.build_dir / "src" / "modules" / "simulation" / "gz_plugins"

    @property
    def gz_server_config(self) -> Path:
        return self.px4_dir / "src" / "modules" / "simulation" / "gz_bridge" / "server.config"

    def missing(self) -> list[Path]:
        """Required files that don't exist, for a clear error before launching anything."""
        required = [self.px4_bin, self.px4_etc, self.gz_models, self.gz_worlds, self.gz_plugins]
        return [p for p in required if not p.exists()]
