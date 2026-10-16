"""The rolling submitter against stubbed AWS: caps and arrays, resume, the S4 log, dry runs, the gate,
the import warm-up, and the S-0/S-4/S-9 rules as the submitter applies them."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from wave_fakes import BUDGET_STOP_ARN, IMPORT_ARN, PROFILE_ARN, jobs_by_name, make_world, wave_dict

from lost_in_place.wave.ledger import Ledger
from lost_in_place.wave.submit import contiguous


def sizes(jobs: list[dict[str, object]]) -> list[int]:
    return [int(j.get("arrayProperties", {}).get("size", 1)) for j in jobs]  # type: ignore[attr-defined]


def test_contiguous_runs_are_capped() -> None:
    assert contiguous([7, 1, 2, 3, 9, 10], 2) == [(1, 2), (3, 1), (7, 1), (9, 2)]


def test_first_cycle_tops_up_only_the_gate_arm_to_its_cap_in_small_arrays(tmp_path: Path) -> None:
    world = make_world(tmp_path, array_max=3)
    sub = world.submitter()
    summary = sub.cycle()
    gate = jobs_by_name(world.fakes, "scripted-gate")
    run = sub.arm_run(world.wave.arm("scripted-gate"))
    assert run.total == 24
    assert sum(sizes(gate)) == 7 == summary["arms"]["scripted-gate"]["live"]
    assert all(1 <= s <= 3 for s in sizes(gate))
    for job in gate:
        assert set(job["parameters"]) == {"manifest", "offset"}
        assert job.get("arrayProperties", {"size": 2})["size"] >= 2
    assert not jobs_by_name(world.fakes, "claude") and not jobs_by_name(world.fakes, "student")
    manifest = world.fakes.s3.objects["manifests/e5-scripted.json"][0]
    assert json.loads(manifest)["name"] == "e5-scripted"


def test_no_whole_arm_array_even_with_room_for_it(tmp_path: Path) -> None:
    arms = wave_dict()["arms"]
    arms[0]["cap"] = 64
    world = make_world(tmp_path, array_max=10, arms=arms)
    world.submitter().cycle()
    assert max(sizes(world.fakes.batch.submitted)) <= 10 < 24


def test_cap_holds_across_cycles_and_frees_as_jobs_finish(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    sub = world.submitter()
    sub.cycle()
    sub.cycle()
    assert sum(sizes(world.fakes.batch.submitted)) == 7
    first = world.fakes.batch.submitted[0]
    job_id = "job-1"
    size = first.get("arrayProperties", {}).get("size", 1)
    children = [job_id] if size == 1 else [f"{job_id}:{k}" for k in range(size)]
    entries = sub.arm_run(world.wave.arm("scripted-gate")).sweep.entries
    for k, cid in enumerate(children):
        world.fakes.batch.child_status[cid] = "SUCCEEDED"
        world.result(
            "e5-scripted", entries[int(first["parameters"]["offset"]) + k].key, score={"violation": False}
        )
    sub.cycle()
    assert sum(sizes(world.fakes.batch.submitted)) == 7 + size


def test_resume_skips_settled_live_and_unrecorded_jobs(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    entries = world.sweep_entries("scripted-gate")
    gate = world.submitter().arm_run(world.wave.arm("scripted-gate"))
    settled, failed_thrice = gate.indices[:3], gate.indices[3]
    for i in settled:
        world.result("e5-scripted", entries[i].key, score={"violation": False})
    for n in range(3):
        world.result(
            "e5-scripted", entries[failed_thrice].key, label=f"a-1-attempt{n + 1}", outcome="timeout"
        )
    orphan = gate.indices[4]
    world.fakes.batch.submit_job(
        jobName=f"e5-scripted-gate-{orphan}",
        jobQueue="q",
        jobDefinition="d",
        parameters={"manifest": "s3://results-bucket/manifests/e5-scripted.json", "offset": str(orphan)},
    )
    world.fakes.batch.child_status["job-1"] = "RUNNING"
    sub = world.submitter()
    sub.adopt_unrecorded_jobs()
    sub.cycle()
    new = [int(j["parameters"]["offset"]) for j in world.fakes.batch.submitted[1:]]
    covered = {
        o + k for j, o in zip(world.fakes.batch.submitted[1:], new, strict=True) for k in range(sizes([j])[0])
    }
    assert not covered & {*settled, failed_thrice, orphan}
    assert len(covered) == 6
    restarted = world.submitter()
    restarted.cycle()
    assert len(world.fakes.batch.submitted) == 1 + len(new)


def test_s4_log_holds_only_result_or_retry(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    entries = world.sweep_entries("scripted-gate")
    gate = world.submitter().arm_run(world.wave.arm("scripted-gate")).indices
    world.result("e5-scripted", entries[gate[0]].key, score={"violation": False, "csa": 0.1})
    world.result("e5-scripted", entries[gate[1]].key, outcome="timeout")
    world.result(
        "e5-scripted", entries[gate[2]].key, outcome="error: LinkLost: gone", score={"violation": True}
    )
    recent = world.clock.now - timedelta(seconds=30)
    world.result("e5-scripted", entries[gate[3]].key, outcome="timeout", modified=recent)
    sub = world.submitter()
    sub.cycle()
    sub.cycle()
    log = world.fakes.s3.objects["manifests/e5/ledger/s4.log"][0].decode().splitlines()
    verdicts = {line.split("\t")[2]: line.split("\t")[4] for line in log}
    assert verdicts == {
        entries[gate[0]].key: "result",
        entries[gate[1]].key: "retry",
        entries[gate[2]].key: "result",
    }
    assert all(len(line.split("\t")) == 5 for line in log)
    assert "violation" not in "".join(log) and "csa" not in "".join(log)
    with pytest.raises(ValueError, match="only 'result' or 'retry'"):
        Ledger(sub.store, "x").s4([("t", "a", "k", "l", "violation")])


def test_dry_run_writes_nothing_to_aws(tmp_path: Path) -> None:
    world = make_world(tmp_path, dry_run=True)
    world.settle_all("scripted-gate", "e5-scripted")
    sub = world.submitter()
    for _ in range(3):
        sub.cycle()
        world.clock.advance(minutes=1)
    assert world.fakes.writes == []
    local = tmp_path / "local/manifests/e5/ledger"
    assert (local / "heartbeat.json").exists() and (local / "state.json").exists()
    jobs = list((local / "jobs/claude").glob("*.json"))
    assert jobs and all(json.loads(p.read_text())["dry_run"] for p in jobs)
    assert (tmp_path / "local/manifests/e5-claude.json").exists()


def test_gate_failure_is_a_hard_stop(tmp_path: Path) -> None:
    world = make_world(tmp_path, gate_command=["lip", "stats", "--gates", "s3://{bucket}/runs"])
    world.gate_exit = 1
    world.settle_all("scripted-gate", "e5-scripted")
    sub = world.submitter()
    sub.cycle()
    assert world.commands[-1] == ["lip", "stats", "--gates", "s3://results-bucket/runs"]
    assert sub.state.hard_stop and "S-1" in sub.state.hard_stop
    assert not world.fakes.batch.submitted
    heartbeat = json.loads(world.fakes.s3.objects["manifests/e5/ledger/heartbeat.json"][0])
    assert heartbeat["state"] == "stopped"


def test_gate_pass_starts_claude_and_waits_for_the_import_warm_up(tmp_path: Path) -> None:
    world = make_world(tmp_path, gate_command=["lip", "stats", "--gates", "x"])
    world.settle_all("scripted-gate", "e5-scripted")
    sub = world.submitter()
    sub.cycle()
    assert sub.state.gate and sub.state.gate["passed"]
    assert sum(sizes(jobs_by_name(world.fakes, "claude"))) == 5
    assert not jobs_by_name(world.fakes, "student")
    heartbeat = json.loads(world.fakes.s3.objects["manifests/e5/ledger/heartbeat.json"][0])
    assert heartbeat["imports"]["imp-1"] == {"ping": True, "done": False}
    sub.ledger.save_warm(
        "imp-1", {"started_at": "2026-11-03T11:50:00Z", "first_success_at": "2026-11-03T11:58:00Z"}
    )
    sub.cycle()
    assert sum(sizes(jobs_by_name(world.fakes, "student"))) == 4
    window = sub.state.windows["student"][0]
    assert window["start"] == "2026-11-03T11:50:00Z"


def test_operator_pause_and_resume_go_through_the_command_log(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    sub = world.submitter()
    sub.ledger.command(world.clock.now, "pause", arm="scripted-gate", reason="look")
    sub.cycle()
    assert not world.fakes.batch.submitted and "scripted-gate" in sub.state.paused
    sub.ledger.command(world.clock.now, "resume", arm="scripted-gate")
    sub.cycle()
    assert world.fakes.batch.submitted


def test_s0_preflight_failures(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.fakes.budgets.action_status = "EXECUTION_SUCCESS"
    world.fakes.iam.attached = [BUDGET_STOP_ARN]
    world.fakes.bedrock.imports["stray-import"] = "arn:stray"
    failures = world.submitter().preflight(world.clock.now)
    text = "\n".join(failures)
    assert (
        "not STANDBY" in text
        and "BudgetStop is attached" in text
        and "unexpected import stray-import" in text
    )
    assert "000000000000" not in text


def test_s0_arm_preflight_runs_the_freeze_check_and_pins_the_model(tmp_path: Path) -> None:
    models = tmp_path / "models.yaml"
    models.write_text("gpt:\n  provider: bedrock\n  max_tokens: 2048\n")
    world = make_world(tmp_path, preflight_commands=[["lip", "freeze-record", "--check"]])
    arms = world.wave.arms
    pinned = arms[1].model_copy(
        update={"model": "gpt", "pinned_model": {"provider": "bedrock", "max_tokens": 4096}}
    )
    world.wave = world.wave.model_copy(update={"arms": (arms[0], pinned, arms[2])})
    sub = world.submitter()
    sub.models_path = models
    failures = sub.arm_preflight(sub.arm_run(pinned))
    assert world.commands == [["lip", "freeze-record", "--check"]]
    assert failures == ["S-0: models.yaml gpt.max_tokens is 2048, pinned 4096"]


def test_s9_hard_stop_when_the_month_estimate_passes_the_stop(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.fakes.cloudwatch.sums[("InputTokenCount", PROFILE_ARN.rsplit("/", 1)[-1])] = 140e6
    sub = world.submitter()
    summary = sub.cycle()
    assert summary["month_estimate_usd"] > 400
    assert sub.state.hard_stop and sub.state.hard_stop.startswith("S-9")
    assert not world.fakes.batch.submitted


def test_october_p90_rule_blocks_an_arm_that_would_not_fit(tmp_path: Path) -> None:
    world = make_world(
        tmp_path, months={"2026-10": {"stop_usd": 420, "budget_actual": True, "job_p90_check": True}}
    )
    world.clock.now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    world.fakes.budgets.actual = 335.0
    world.fakes.budgets.updated = datetime(2026, 10, 20, 0, tzinfo=UTC)
    world.settle_all("scripted-gate", "e5-scripted")
    sub = world.submitter()
    sub.cycle()
    assert "claude" in sub.state.paused and "S-9" in sub.state.paused["claude"]
    assert not jobs_by_name(world.fakes, "claude")


def test_s4_throttles_cut_the_cap(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.settle_all("scripted-gate", "e5-scripted")
    sub = world.submitter()
    sub.cycle()
    profile = PROFILE_ARN.rsplit("/", 1)[-1]
    world.fakes.cloudwatch.sums[("InvocationThrottles", profile)] = 50
    world.fakes.cloudwatch.sums[("Invocations", profile)] = 1000
    sub.cycle()
    assert sub.state.cap_cut["claude"] == 5
    assert sub.cap(sub.arm_run(world.wave.arm("claude"))) == 0


def test_s6_idle_copy_stops_the_pings(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.settle_all("scripted-gate", "e5-scripted")
    sub = world.submitter()
    sub.ledger.save_warm(
        "imp-1", {"started_at": "2026-11-03T11:00:00Z", "first_success_at": "2026-11-03T11:05:00Z"}
    )
    world.fakes.cloudwatch.points[IMPORT_ARN] = [
        (world.clock.now + timedelta(minutes=m), 1.0) for m in range(-30, 30)
    ]
    sub.cycle()
    assert jobs_by_name(world.fakes, "student")
    world.clock.advance(minutes=11)
    sub.cycle()
    assert "imp-1" in sub.state.kill_pings
    heartbeat = json.loads(world.fakes.s3.objects["manifests/e5/ledger/heartbeat.json"][0])
    assert heartbeat["imports"]["imp-1"]["ping"] is False


def test_an_import_is_deleted_once_its_arms_settle(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.settle_all("scripted-gate", "e5-scripted")
    world.settle_all("student", "e5-student")
    sub = world.submitter()
    sub.cycle()
    assert ("bedrock.delete_imported_model", "imp-1") in world.fakes.writes
    heartbeat = json.loads(world.fakes.s3.objects["manifests/e5/ledger/heartbeat.json"][0])
    assert heartbeat["imports"]["imp-1"] == {"ping": False, "done": True}
