"""Truth-based metrics for one episode, computed from the PX4 ULog.

Ground truth is `vehicle_local_position_groundtruth` (SITL only); the estimate is
`vehicle_local_position`. With no heading reference the two frames differ by the spawn yaw,
so errors are reported "take-off aligned": the EKF track is rotated and translated onto the
truth track at take-off.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from pyulog import ULog

Array = NDArray[np.float64]

TAKEOFF_HEIGHT_M = 0.3
IMPACT_SPEED_MPS = 1.0
FALSE_ZERO_SPEED_MPS = 0.3


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= successes <= n:
        raise ValueError("successes must be between 0 and n")
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def wrap_angle(a: Array) -> Array:
    return np.asarray((a + np.pi) % (2 * np.pi) - np.pi, dtype=np.float64)


def align_to_truth(
    ekf_x: Array,
    ekf_y: Array,
    ekf_heading: Array,
    truth_x: Array,
    truth_y: Array,
    truth_heading: Array,
    i0: int,
) -> tuple[Array, Array, float]:
    """Rotate and translate the EKF track so it coincides with truth at index i0.

    Returns the aligned x, y and the heading offset (truth - EKF) in radians.
    """
    dpsi = float(wrap_angle(np.array([truth_heading[i0] - ekf_heading[i0]]))[0])
    c, s = math.cos(dpsi), math.sin(dpsi)
    dx, dy = ekf_x - ekf_x[i0], ekf_y - ekf_y[i0]
    return c * dx - s * dy + truth_x[i0], s * dx + c * dy + truth_y[i0], dpsi


def _topic(ulog: ULog, name: str) -> dict[str, Array] | None:
    for d in ulog.data_list:
        if d.name == name and d.multi_id == 0:
            return {k: np.asarray(v) for k, v in d.data.items()}
    return None


def analyze(run_dir: Path) -> dict[str, Any]:
    """Compute metrics for an episode directory and write `metrics.json` next to it."""
    episode = json.loads((run_dir / "episode.json").read_text())
    m: dict[str, Any] = {k: episode[k] for k in ("scenario_id", "seed", "world", "outcome", "failsafe")}
    ulog = ULog(str(run_dir / "flight.ulg"))
    t0 = float(ulog.start_timestamp)

    def secs(d: dict[str, Array]) -> Array:
        return (d["timestamp"].astype(np.float64) - t0) / 1e6

    lp = _topic(ulog, "vehicle_local_position")
    gt = _topic(ulog, "vehicle_local_position_groundtruth")
    status = _topic(ulog, "vehicle_status")
    flow = _topic(ulog, "vehicle_optical_flow")
    if lp is None or gt is None or status is None:
        raise ValueError("ULog lacks local position, ground truth or vehicle status")
    t = secs(lp)
    g = {k: np.interp(t, secs(gt), gt[k]) for k in ("x", "y", "z", "vx", "vy", "heading")}

    armed = np.flatnonzero(status["arming_state"] == 2)
    if not len(armed):
        m["error"] = "never armed"
        return _write(run_dir, m)
    i_arm = int(np.searchsorted(t, secs(status)[armed[0]]))
    m["eph_at_arm_m"] = round(float(lp["eph"][i_arm]), 3)
    rel_z = g["z"] - g["z"][i_arm]
    up = np.flatnonzero((t > t[i_arm]) & (rel_z < -TAKEOFF_HEIGHT_M))
    if not len(up):
        m["error"] = "never took off"
        return _write(run_dir, m)
    i0 = int(up[0])
    down = np.flatnonzero((t > t[i0] + 2) & (rel_z > -TAKEOFF_HEIGHT_M))
    i1 = int(down[0]) if len(down) else len(t) - 1
    air = slice(i0, i1)

    rx, ry, dpsi = align_to_truth(lp["x"], lp["y"], lp["heading"], g["x"], g["y"], g["heading"], i0)
    gap = np.hypot(rx - g["x"], ry - g["y"])
    true_exc = np.hypot(g["x"] - g["x"][i0], g["y"] - g["y"][i0])
    ekf_exc = np.hypot(lp["x"] - lp["x"][i0], lp["y"] - lp["y"][i0])
    speed = np.hypot(g["vx"], g["vy"])

    m["airborne_s"] = round(float(t[i1] - t[i0]), 1)
    m["heading_offset_deg"] = round(math.degrees(dpsi), 1)
    m["belief_gap_max_m"] = round(float(gap[air].max()), 2)
    m["true_excursion_max_m"] = round(float(true_exc[air].max()), 2)
    m["peak_speed_mps"] = round(float(speed[air].max()), 2)
    m["xy_valid_frac"] = round(float(lp["xy_valid"][air].mean()), 3)
    for dt in (10, 20, 30):
        j = int(np.searchsorted(t, t[i0] + dt))
        if j < i1:
            m[f"true_drift_{dt}s_m"] = round(float(true_exc[j]), 2)
            m[f"ekf_drift_{dt}s_m"] = round(float(ekf_exc[j]), 2)

    # Event sim times are PX4 boot time; ULog times here are relative to the log start.
    end_cmd = [e["sim_s"] - t0 / 1e6 for e in episode["events"] if e.get("action") == "end"]
    impact = len(down) > 0 and speed[i1] > IMPACT_SPEED_MPS and (not end_cmd or t[i1] < end_cmd[0])
    m["ground_impact"] = bool(impact)

    if flow is not None:
        tf = secs(flow)
        in_air = (tf > t[i0]) & (tf < t[i1])
        q = flow["quality"][in_air]
        moving = np.interp(tf, t, speed)[in_air] > FALSE_ZERO_SPEED_MPS
        zero = np.hypot(flow["pixel_flow[0]"], flow["pixel_flow[1]"])[in_air] < 1e-6
        m["flow_quality_mean"] = round(float(q.mean()), 1) if len(q) else None
        fused_zero = zero & (q > 0)  # zero flow reported with nonzero quality, so EKF2 can fuse it
        m["false_zero_frac_moving"] = round(float(fused_zero[moving].mean()), 3) if moving.any() else None

    failsafes = [(x.timestamp - t0) / 1e6 for x in ulog.logged_messages if "failsafe" in x.message.lower()]
    after = [f for f in failsafes if f > t[i0]]
    m["first_failsafe_after_takeoff_s"] = round(after[0] - t[i0], 1) if after else None
    return _write(run_dir, m)


def _json_default(value: object) -> object:
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def _write(run_dir: Path, m: dict[str, Any]) -> dict[str, Any]:
    (run_dir / "metrics.json").write_text(json.dumps(m, indent=1, default=_json_default))
    return m
