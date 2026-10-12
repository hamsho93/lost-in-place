import json
from pathlib import Path

import pytest
from mavsdk_grpc.failure import FailureType, FailureUnit

from lost_in_place import episode
from lost_in_place.episode import failure_enums
from lost_in_place.scenario import End, Inject, Scenario


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


def test_crashed_episode_is_recorded_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ulog = tmp_path / "px4" / "log.ulg"
    ulog.parent.mkdir()
    ulog.write_bytes(b"ULog")

    class FakeSim:
        def __init__(self, **kwargs: object) -> None:
            self.started = 0

        def start(self, console_log: Path) -> None:
            self.started += 1
            console_log.write_text("px4 console\n")

        def stop(self) -> None:
            pass

        def latest_ulog(self) -> Path:
            return ulog

    class CrashingEpisode:
        def __init__(self, *args: object) -> None:
            pass

        def mark(self, name: str) -> None:
            pass

        async def run(self) -> str:
            raise RuntimeError("Stream removed\n(recvmsg: Connection reset by peer)")

    class FakePaths:
        def missing(self) -> list[Path]:
            return []

    monkeypatch.setattr(episode, "SimInstance", FakeSim)
    monkeypatch.setattr(episode, "_Episode", CrashingEpisode)
    scenario = Scenario(id="crash", world="lip_textured", steps=[End(at_s=1)])

    result = episode.run_episode(scenario, 0, tmp_path / "out", paths=FakePaths())  # type: ignore[arg-type]

    assert result.outcome == "error: RuntimeError: Stream removed"
    assert result.boot_attempts == 1
    saved = json.loads((tmp_path / "out" / "episode.json").read_text())
    assert saved["outcome"] == result.outcome
    assert (tmp_path / "out" / "flight.ulg").read_bytes() == b"ULog"
    assert (tmp_path / "out" / "px4_console.txt").exists()
