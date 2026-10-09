import math

import numpy as np
import pytest

from lost_in_place.metrics import align_to_truth, wilson_interval, wrap_angle


def test_wilson_interval_known_values() -> None:
    low, high = wilson_interval(10, 20)
    assert low == pytest.approx(0.2993, abs=1e-3)
    assert high == pytest.approx(0.7007, abs=1e-3)
    assert wilson_interval(0, 20)[0] == 0.0
    assert wilson_interval(20, 20)[1] == 1.0


@pytest.mark.parametrize(("k", "n"), [(1, 0), (-1, 5), (6, 5)])
def test_wilson_interval_rejects_bad_counts(k: int, n: int) -> None:
    with pytest.raises(ValueError):
        wilson_interval(k, n)


def test_wrap_angle() -> None:
    out = wrap_angle(np.array([0.0, math.pi + 0.1, -math.pi - 0.1, 4 * math.pi]))
    assert out == pytest.approx([0.0, -math.pi + 0.1, math.pi - 0.1, 0.0])


def test_align_recovers_a_rotated_and_shifted_track() -> None:
    # Truth: fly 3 m east from (5, 5). The EKF frame is rotated 90 degrees (nose-at-boot north).
    s = np.linspace(0, 3, 50)
    truth_x, truth_y = 5 + 0 * s, 5 + s
    ekf_x, ekf_y = s, 0 * s
    truth_heading = np.full_like(s, math.pi / 2)
    ekf_heading = np.zeros_like(s)
    rx, ry, dpsi = align_to_truth(ekf_x, ekf_y, ekf_heading, truth_x, truth_y, truth_heading, 0)
    assert math.degrees(dpsi) == pytest.approx(90)
    assert np.hypot(rx - truth_x, ry - truth_y).max() == pytest.approx(0, abs=1e-9)


def test_numpy_scalars_serialise(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from lost_in_place.metrics import _write

    _write(tmp_path, {"flag": np.bool_(True), "x": np.float32(1.5)})
    assert (tmp_path / "metrics.json").read_text().replace(" ", "").replace(
        "\n", ""
    ) == '{"flag":true,"x":1.5}'
