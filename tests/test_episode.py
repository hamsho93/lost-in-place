import pytest
from mavsdk_grpc.failure import FailureType, FailureUnit

from lost_in_place.episode import failure_enums
from lost_in_place.scenario import Inject


@pytest.mark.parametrize(
    ("unit", "ftype", "expected"),
    [
        ("optical_flow", "off", (FailureUnit.SENSOR_OPTICAL_FLOW, FailureType.OFF)),
        ("distance_sensor", "stuck", (FailureUnit.SENSOR_DISTANCE_SENSOR, FailureType.STUCK)),
        ("battery", "wrong", (FailureUnit.SYSTEM_BATTERY, FailureType.WRONG)),
    ],
)
def test_failure_enums(unit: str, ftype: str, expected: tuple[FailureUnit, FailureType]) -> None:
    assert failure_enums(Inject(at_s=0, unit=unit, type=ftype)) == expected  # type: ignore[arg-type]
