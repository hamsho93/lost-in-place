import json
import logging
from pathlib import Path

import pytest
from mavsdk_grpc.failure import FailureType, FailureUnit

from lost_in_place import episode
from lost_in_place.episode import failure_enums
from lost_in_place.scenario import End, Inject, Scenario, SetParam


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

        def real_time_factor(self) -> float:
            return 0.3

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

        def real_time_factor(self) -> float:
            return 0.3

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


def test_rejected_fault_ends_the_episode() -> None:
    import asyncio

    ep = _episode_at_rest(None)
    ep.connected.set()

    class Failure:
        async def inject(self, *args: object) -> None:
            raise RuntimeError("UNSUPPORTED: 'Unsupported'")

    class Drone:
        failure = Failure()

    with pytest.raises(episode.InjectFailed, match="optical_flow off: UNSUPPORTED"):
        asyncio.run(ep._apply(Drone(), Inject(at_s=20, unit="optical_flow", type="off")))  # type: ignore[arg-type]
    assert ep.result.events[-1]["ack"] == "UNSUPPORTED: 'Unsupported'"


def test_a_flow_scale_fault_sets_the_scale_parameter() -> None:
    import asyncio

    ep = _episode_at_rest(None)
    ep.connected.set()
    calls: list[tuple[str, float]] = []

    class Param:
        async def set_param_float(self, name: str, value: float) -> None:
            calls.append((name, value))

    class Drone:
        param = Param()

    asyncio.run(ep._apply(Drone(), Inject(at_s=2, unit="optical_flow", type="scale", value=0.7)))  # type: ignore[arg-type]
    assert calls == [("SENS_FLOW_SCALE", 0.7)]
    assert ep.result.events[-1] | {"wall_s": 0} == {
        "wall_s": 0,
        "sim_s": 0.0,
        "action": "inject",
        "unit": "optical_flow",
        "type": "scale",
        "ack": "accepted",
        "value": 0.7,
    }


def test_param_steps_set_int_parameters_as_ints() -> None:
    import asyncio

    ep = _episode_at_rest(None)
    ep.connected.set()
    calls: list[tuple[str, object]] = []

    class Param:
        async def set_param_int(self, name: str, value: int) -> None:
            calls.append((name, value))

        async def set_param_float(self, name: str, value: float) -> None:
            calls.append((name, value))

    class Drone:
        param = Param()

    for step in (
        SetParam(at_s=0, name="EKF2_NOAID_TOUT", value=10_000_000, type="int"),
        SetParam(at_s=0, name="COM_POS_FS_EPH", value=12.5),
    ):
        asyncio.run(ep._apply(Drone(), step))  # type: ignore[arg-type]
    assert calls == [("EKF2_NOAID_TOUT", 10_000_000), ("COM_POS_FS_EPH", 12.5)]
    assert all(type(v) is t for (_, v), t in zip(calls, (int, float), strict=True))
    with pytest.raises(ValueError, match="int parameter"):
        SetParam(at_s=0, name="EKF2_NOAID_TOUT", value=1.5, type="int")


def test_provenance_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIP_GIT_SHA", "abc123")
    prov = episode.provenance.collect('{"id": "x"}')
    assert prov["git_sha"] == "abc123"
    assert prov["px4"]["commit"] != "unknown"
    assert set(prov["px4"]["patches"]) >= {"0001-gz-optical-flow-skip-rate-limited-frames.patch"}
    assert len(prov["scenario_sha256"]) == 16


def test_image_digest_is_the_pinned_registry_digest() -> None:
    repo = "123.dkr.ecr.us-east-1.amazonaws.com/images"
    pinned = {"Image": f"{repo}@sha256:a0512efa", "ImageID": "sha256:40e7368d"}
    assert episode.provenance.digest_from_task_metadata(pinned) == "sha256:a0512efa"
    tagged = {"Image": f"{repo}:latest", "ImageID": "sha256:40e7368d"}
    assert episode.provenance.digest_from_task_metadata(tagged) == "sha256:40e7368d"


def test_a_model_planner_gets_wall_time_for_each_budgeted_call() -> None:
    from lost_in_place.agent import AgentConfig
    from lost_in_place.scenario import load_scenario

    scenario = load_scenario(Path("scenarios/e2/b00_healthy.yaml"))
    assert scenario.mission is not None
    llm = AgentConfig(planner="llm", planner_options={"model": "qwen3-4b-zero-shot"})
    assert episode.episode_timeout_wall_s(scenario, llm) == (
        episode.EPISODE_TIMEOUT_WALL_S + scenario.mission.planner_calls * episode.PLANNER_CALL_WALL_S
    )
    assert episode.episode_timeout_wall_s(scenario, AgentConfig()) == episode.EPISODE_TIMEOUT_WALL_S


def test_the_longest_model_episode_fits_inside_the_batch_job_timeout() -> None:
    from lost_in_place.scenario import Mission

    worst = episode.EPISODE_TIMEOUT_WALL_S + Mission.model_fields["planner_calls"].default * (
        episode.PLANNER_CALL_WALL_S
    )
    # infra's EPISODE_TIMEOUT is 90 min; leave room for a stalled first boot and the upload.
    assert worst + episode.EPISODE_TIMEOUT_WALL_S <= 90 * 60
