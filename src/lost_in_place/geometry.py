"""Frames and polygons shared by scenarios, validators and the scorer.

Truth frame: PX4's `vehicle_local_position_groundtruth`, metres (north, east) from the Gazebo world
origin. North is Gazebo +y and east is Gazebo +x.

Take-off frame: the frame the estimator and the planner work in. On a vehicle without a compass,
EKF2 sets north to the nose direction at boot and the origin to where it booted, so a vehicle
spawned at Gazebo pose (x, y, yaw) has its take-off frame at truth (y, x), rotated by its truth
heading 90 - yaw degrees.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Point = tuple[float, float]  # (north, east)

# The Gazebo world origin (PX4's default.sdf, which every generated world keeps).
WORLD_ORIGIN_LATLON = (47.397971057728974, 8.546163739800146)
EARTH_RADIUS_M = 6371000.0  # PX4's CONSTANTS_RADIUS_OF_EARTH


def latlon_to_truth(
    lat_deg: float, lon_deg: float, origin: tuple[float, float] = WORLD_ORIGIN_LATLON
) -> Point:
    """PX4's MapProjection::project: ground-truth lat/lon back to truth-frame metres."""
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    lat0, lon0 = math.radians(origin[0]), math.radians(origin[1])
    cos_d_lon = math.cos(lon - lon0)
    arg = max(-1.0, min(1.0, math.sin(lat0) * math.sin(lat) + math.cos(lat0) * math.cos(lat) * cos_d_lon))
    c = math.acos(arg)
    k = c / math.sin(c) if abs(c) > 0 else 1.0
    north = k * (math.cos(lat0) * math.sin(lat) - math.sin(lat0) * math.cos(lat) * cos_d_lon) * EARTH_RADIUS_M
    return north, k * math.cos(lat) * math.sin(lon - lon0) * EARTH_RADIUS_M


def spawn_pose(x: float, y: float, yaw_deg: float) -> tuple[Point, float]:
    """Truth position and heading (radians, clockwise from north) of a Gazebo spawn pose."""
    return (y, x), math.radians(90.0 - yaw_deg)


def takeoff_to_truth(p: Point, origin: Point, heading: float) -> Point:
    c, s = math.cos(heading), math.sin(heading)
    return origin[0] + c * p[0] - s * p[1], origin[1] + s * p[0] + c * p[1]


def truth_to_takeoff(p: Point, origin: Point, heading: float) -> Point:
    c, s = math.cos(heading), math.sin(heading)
    dn, de = p[0] - origin[0], p[1] - origin[1]
    return c * dn + s * de, -s * dn + c * de


def contains(polygon: Sequence[Point], p: Point) -> bool:
    """Ray casting; points on an edge may go either way, so callers use a buffer."""
    inside = False
    n = len(polygon)
    for i in range(n):
        (a_n, a_e), (b_n, b_e) = polygon[i], polygon[(i + 1) % n]
        if (a_e > p[1]) != (b_e > p[1]):
            cross_n = a_n + (p[1] - a_e) * (b_n - a_n) / (b_e - a_e)
            if p[0] < cross_n:
                inside = not inside
    return inside


def _segment_distance(p: Point, a: Point, b: Point) -> float:
    dn, de = b[0] - a[0], b[1] - a[1]
    length2 = dn * dn + de * de
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dn + (p[1] - a[1]) * de) / length2))
    return math.hypot(p[0] - (a[0] + t * dn), p[1] - (a[1] + t * de))


def edge_distance(polygon: Sequence[Point], p: Point) -> float:
    n = len(polygon)
    return min(_segment_distance(p, polygon[i], polygon[(i + 1) % n]) for i in range(n))


def inside_with_margin(polygon: Sequence[Point], p: Point, margin: float) -> bool:
    """Inside the polygon and at least `margin` metres from its edges."""
    return contains(polygon, p) and edge_distance(polygon, p) >= margin


def touches(polygon: Sequence[Point], p: Point, margin: float) -> bool:
    """Inside the polygon or within `margin` metres of it."""
    return contains(polygon, p) or edge_distance(polygon, p) < margin


def segment_points(a: Point, b: Point, step: float = 0.1) -> list[Point]:
    """Points along a straight path, `step` metres apart, including both ends."""
    n = max(1, math.ceil(math.hypot(b[0] - a[0], b[1] - a[1]) / step))
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]
