import math

import pytest

from lost_in_place import geometry

SQUARE = [(0.0, 0.0), (0.0, 2.0), (2.0, 2.0), (2.0, 0.0)]


def test_spawn_facing_east_has_takeoff_north_along_truth_east() -> None:
    origin, heading = geometry.spawn_pose(x=1.0, y=-2.0, yaw_deg=0.0)
    assert origin == (-2.0, 1.0) and heading == pytest.approx(math.pi / 2)
    n, e = geometry.takeoff_to_truth((1.0, 0.0), origin, heading)
    assert (n, e) == (pytest.approx(-2.0), pytest.approx(2.0))
    back = geometry.truth_to_takeoff((n, e), origin, heading)
    assert back == (pytest.approx(1.0), pytest.approx(0.0))


def test_polygon_tests() -> None:
    assert geometry.contains(SQUARE, (1.0, 1.0)) and not geometry.contains(SQUARE, (3.0, 1.0))
    assert geometry.edge_distance(SQUARE, (1.0, 1.5)) == pytest.approx(0.5)
    assert geometry.inside_with_margin(SQUARE, (1.0, 1.0), 0.9)
    assert not geometry.inside_with_margin(SQUARE, (1.0, 1.9), 0.4)
    assert geometry.touches(SQUARE, (2.3, 1.0), 0.4) and not geometry.touches(SQUARE, (2.5, 1.0), 0.4)
    assert len(geometry.segment_points((0, 0), (1, 0), step=0.25)) == 5
