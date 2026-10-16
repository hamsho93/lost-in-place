"""The import pinger: it pings only while a live, running submitter asks for it (dead-man switch)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from wave_fakes import IMPORT_ARN, Clock, FakeRuntime

from lost_in_place.wave.config import Rules
from lost_in_place.wave.ledger import Ledger, iso
from lost_in_place.wave.pinger import Pinger, invoke_ping
from lost_in_place.wave.store import LocalStore


def setup(tmp_path: Path, not_ready: int = 0) -> tuple[Ledger, Clock, FakeRuntime, Pinger]:
    ledger = Ledger(LocalStore(tmp_path), "ledger")
    clock = Clock()
    runtime = FakeRuntime([], not_ready=not_ready)
    pinger = Pinger(
        ledger,
        "imp-1",
        lambda: IMPORT_ARN,
        Rules(),
        invoke=lambda arn: invoke_ping(runtime, arn),
        clock=clock,
        sleep=lambda _: clock.advance(seconds=30),
        out=lambda _: None,
    )
    return ledger, clock, runtime, pinger


def beat(ledger: Ledger, clock: Clock, ping: bool = True, done: bool = False, state: str = "running") -> None:
    ledger.heartbeat(
        {"at": iso(clock.now), "state": state, "imports": {"imp-1": {"ping": ping, "done": done}}}
    )


def test_no_heartbeat_means_no_ping(tmp_path: Path) -> None:
    _, _, runtime, pinger = setup(tmp_path)
    assert pinger.run() == "deadman"
    assert runtime.calls == []


def test_stale_heartbeat_stops_the_pinger_within_five_minutes(tmp_path: Path) -> None:
    ledger, clock, runtime, pinger = setup(tmp_path)
    beat(ledger, clock)
    start = clock.now
    assert pinger.run() == "deadman"
    assert len(runtime.calls) == 11
    assert clock.now - start <= timedelta(seconds=Rules().deadman_s + 30)


def test_stopped_submitter_stops_the_pinger(tmp_path: Path) -> None:
    ledger, clock, runtime, pinger = setup(tmp_path)
    beat(ledger, clock, state="stopped")
    assert pinger.step() == "deadman" and runtime.calls == []


def test_warm_up_is_recorded_and_pings_wait_for_the_flag(tmp_path: Path) -> None:
    ledger, clock, runtime, pinger = setup(tmp_path, not_ready=2)
    beat(ledger, clock, ping=False)
    assert pinger.step() is None and runtime.calls == []
    beat(ledger, clock)
    for _ in range(3):
        assert pinger.step() is None
        clock.advance(seconds=30)
    warm = ledger.warm("imp-1")
    assert warm is not None
    assert warm["started_at"] == "2026-11-03T12:00:00Z"
    assert warm["first_success_at"] == "2026-11-03T12:01:00Z"
    assert warm["arn"] == IMPORT_ARN and "last_error" not in warm
    beat(ledger, clock, ping=False, done=True)
    assert pinger.step() == "done"


def test_warm_up_fails_after_thirty_minutes_and_can_restart(tmp_path: Path) -> None:
    ledger, clock, runtime, pinger = setup(tmp_path, not_ready=10_000)
    for _ in range(62):
        beat(ledger, clock)
        if pinger.step():
            break
        clock.advance(seconds=30)
    warm = ledger.warm("imp-1")
    assert warm is not None and warm["failed_at"] and not warm.get("first_success_at")
    assert warm["last_error"] == "ModelNotReadyException"
    beat(ledger, clock)
    assert pinger.step() == "failed"
    pinger.restart()
    runtime.not_ready = 0
    assert pinger.step() is None
    assert (ledger.warm("imp-1") or {}).get("first_success_at")


def test_dry_run_pinger_never_invokes(tmp_path: Path) -> None:
    ledger, clock, runtime, pinger = setup(tmp_path)
    pinger.dry_run = True
    beat(ledger, clock)
    assert pinger.step() is None and runtime.calls == []
