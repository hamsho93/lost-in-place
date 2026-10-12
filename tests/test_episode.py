import json
import logging
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
            logging.getLogger("mavsdk_server").info("System discovered")
            raise FakeRpcError()

    class FakePaths:
        def missing(self) -> list[Path]:
            return []

    monkeypatch.setattr(episode, "SimInstance", FakeSim)
    monkeypatch.setattr(episode, "_Episode", CrashingEpisode)
    scenario = Scenario(id="crash", world="lip_textured", steps=[End(at_s=1)])

    result = episode.run_episode(scenario, 0, tmp_path / "out", paths=FakePaths())  # type: ignore[arg-type]

    assert result.outcome == "error: FakeRpcError: Stream removed (recvmsg:Connection reset by peer (104))"
    assert result.boot_attempts == 1
    saved = json.loads((tmp_path / "out" / "episode.json").read_text())
    assert saved["outcome"] == result.outcome
    assert (tmp_path / "out" / "flight.ulg").read_bytes() == b"ULog"
    assert (tmp_path / "out" / "px4_console.txt").exists()
    assert "INFO System discovered" in (tmp_path / "out" / "mavsdk_server.txt").read_text()
    assert not logging.getLogger("mavsdk_server").handlers


class FakeRpcError(Exception):
    """Shaped like grpc.aio.AioRpcError: a multi-line str() and the real message in details()."""

    def __str__(self) -> str:
        return "<AioRpcError of RPC that terminated with:\n\tstatus = StatusCode.UNAVAILABLE\n>"

    def details(self) -> str:
        return "Stream removed (recvmsg:Connection reset by peer (104))"


def _episode_at_rest(range_m: float | None) -> episode._Episode:
    scenario = Scenario(id="hover", world="lip_textured", steps=[End(at_s=60)])
    result = episode.EpisodeResult(scenario_id="hover", seed=0, instance=0, world="lip_textured", spawn={})
    ep = episode._Episode(scenario, 0, None, result)  # type: ignore[arg-type]
    ep.range_m = range_m
    return ep


def test_takeoff_check_passes_once_the_vehicle_has_climbed() -> None:
    ep = _episode_at_rest(0.9)
    ep.check_takeoff(0.18)
    assert ep.result.events[-1]["takeoff_check_rise_m"] == 0.72


def test_takeoff_check_raises_when_the_rangefinder_never_moves() -> None:
    ep = _episode_at_rest(0.18)
    with pytest.raises(episode.StuckOnGround, match=r"rose 0\.00 m"):
        ep.check_takeoff(0.18)


def test_takeoff_check_is_skipped_without_rangefinder_data() -> None:
    ep = _episode_at_rest(None)
    ep.check_takeoff(None)
    assert ep.result.events[-1]["takeoff_check"] == "no rangefinder data"


def test_stuck_vehicle_reboots_the_sim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sims: list[object] = []

    class FakeSim:
        def __init__(self, **kwargs: object) -> None:
            sims.append(self)

        def start(self, console_log: Path) -> None:
            console_log.write_text("px4 console\n")

        def stop(self) -> None:
            pass

        def latest_ulog(self) -> None:
            return None

    runs = iter(["stuck", "completed"])

    class FlakyEpisode:
        def __init__(self, *args: object) -> None:
            pass

        def mark(self, name: str) -> None:
            pass

        async def run(self) -> str:
            if next(runs) == "stuck":
                raise episode.StuckOnGround("rangefinder rose 0.00 m in 8 s after arming")
            return "completed"

    class FakePaths:
        def missing(self) -> list[Path]:
            return []

    monkeypatch.setattr(episode, "SimInstance", FakeSim)
    monkeypatch.setattr(episode, "_Episode", FlakyEpisode)
    scenario = Scenario(id="hover", world="lip_textured", steps=[End(at_s=60)])

    result = episode.run_episode(scenario, 0, tmp_path / "out", paths=FakePaths())  # type: ignore[arg-type]

    assert result.outcome == "completed"
    assert result.boot_attempts == 2
    assert result.retried == ["stuck_on_ground: rangefinder rose 0.00 m in 8 s after arming"]


def test_plain_errors_keep_their_first_line() -> None:
    assert episode._error_text(RuntimeError("first\nsecond")) == "first\nsecond"
