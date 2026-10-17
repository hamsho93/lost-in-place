"""Section 10.4 as the submitter applies it to two month-fit arms (GPT-5.6 Sol, then Claude Sonnet
5.5), and the us-east-2 arm's metering, throttles and S-0 region check."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from wave_fakes import EAST2_PROFILE_ARN, STACK, World, jobs_by_name, make_world, wave_dict

from lost_in_place.wave import config
from lost_in_place.wave.cli import submit_main

EAST2_PROFILE = EAST2_PROFILE_ARN.rsplit("/", 1)[-1]
SONNET_MODEL = "us.test.sonnet"


def fit_arms(gpt_canary: float = 0.05) -> list[dict[str, Any]]:
    arms = wave_dict()["arms"][:2]
    return [
        *arms,
        {
            "name": "gpt",
            "sweep": "sweeps/e5/gpt.yaml",
            "cap": 6,
            "needs_gate": True,
            "month_fit": True,
            "start_after": [{"arm": "claude"}],
            "bedrock_model": "us.test.model",
            "price": {"input_per_mtok": 2.2, "output_per_mtok": 11},
            "probe_usd": 100,
            "canary_usd_per_episode": gpt_canary,
        },
        {
            "name": "sonnet",
            "sweep": "sweeps/e5/sonnet.yaml",
            "cap": 6,
            "needs_gate": True,
            "month_fit": True,
            "start_after": [{"arm": "claude"}, {"arm": "gpt"}],
            "bedrock_model": SONNET_MODEL,
            "price": {"input_per_mtok": 2.2, "output_per_mtok": 11},
            "model": "sonnet",
            "probe_usd": 100,
            "canary_usd_per_episode": 0.05,
        },
    ]


def fit_world(
    tmp_path: Path, fixed: float, december: dict[str, Any] | None = None, gpt_canary: float = 0.05
) -> World:
    months: dict[str, Any] = {"2026-11": {"stop_usd": 400}}
    if december is not None:
        months["2026-12"] = december
    world = make_world(
        tmp_path,
        arms=fit_arms(gpt_canary),
        months=months,
        fixed_remaining_usd=fixed,
        deferral_deadline="2026-11-20",
    )
    world.stack = dataclasses.replace(
        STACK, bedrock_profiles={**STACK.bedrock_profiles, SONNET_MODEL: EAST2_PROFILE_ARN}
    )
    (tmp_path / "src/lost_in_place/llm").mkdir(parents=True)
    (tmp_path / "src/lost_in_place/llm/models.yaml").write_text(
        "sonnet:\n  provider: bedrock\n  model_id: us.test.sonnet\n  region: us-east-2\n"
    )
    world.settle_all("scripted-gate", "e5-scripted")
    world.settle_all("claude", "e5-claude")
    return world


DECEMBER = {"arms": ["gpt", "sonnet"]}


def settle(world: World, arm: str) -> None:
    world.settle_all(arm, f"e5-{arm}")
    for job in world.fakes.batch.jobs.values():
        size = job["size"]
        for cid in [job["jobId"]] if size == 1 else [f"{job['jobId']}:{k}" for k in range(size)]:
            world.fakes.batch.child_status[cid] = "SUCCEEDED"


def test_gpt_runs_and_sonnet_with_gpts_projection_goes_to_december(tmp_path: Path) -> None:
    world = fit_world(tmp_path, fixed=200, december={**DECEMBER, "stop_usd": 370})
    sub = world.submitter()
    sub.cycle()
    assert jobs_by_name(world.fakes, "gpt") and not jobs_by_name(world.fakes, "sonnet")
    gpt, sonnet = sub.state.month_fit["gpt"], sub.state.month_fit["sonnet"]
    assert gpt["decision"] == "run" and "preview" not in gpt and gpt["month"] == "2026-11"
    assert sonnet["decision"] == "defer" and sonnet["preview"]
    assert sonnet["total_usd"] > gpt["total_usd"] + 115 > 400 - 115
    assert any("month fit for sonnet (preview): defer" in line for line in world.output)
    assert not sub.finished()

    settle(world, "gpt")
    sub.cycle()
    assert not jobs_by_name(world.fakes, "sonnet")
    final = sub.state.month_fit["sonnet"]
    assert final["decision"] == "defer" and "preview" not in final
    assert "next month's budget covers" in final["reason"]
    assert sub.finished() and sub.waiting() == ["sonnet"]


def test_both_run_in_november_when_they_fit(tmp_path: Path) -> None:
    world = fit_world(tmp_path, fixed=100)
    sub = world.submitter()
    sub.cycle()
    assert sub.state.month_fit["sonnet"]["decision"] == "run"
    settle(world, "gpt")
    sub.cycle()
    assert jobs_by_name(world.fakes, "sonnet")
    assert sub.state.month_fit["sonnet"]["decision"] == "run"


def test_gpt_deferred_holds_sonnet_back_until_december(tmp_path: Path) -> None:
    world = fit_world(tmp_path, fixed=300, december=DECEMBER)
    sub = world.submitter()
    sub.cycle()
    assert not jobs_by_name(world.fakes, "gpt") and not jobs_by_name(world.fakes, "sonnet")
    assert sub.state.month_fit["gpt"]["decision"] == "defer"
    assert "waiting for a next-month budget" in sub.state.month_fit["gpt"]["reason"]
    assert sub.state.month_fit["sonnet"]["preview"]
    assert sub.finished() and sub.waiting() == ["gpt", "sonnet"]

    world.clock.now = datetime(2026, 12, 1, 9, tzinfo=UTC)
    sub = world.submitter()
    sub.cycle()
    assert not jobs_by_name(world.fakes, "gpt")
    blockers = dict(sub.blockers(sub.arm_run(world.wave.arm("gpt")), world.clock.now))
    assert blockers["month"] == "S-9: no approved budget for 2026-12"
    assert sub.state.hard_stop is None

    months = {**world.wave.months, "2026-12": config.Month(stop_usd=370, arms=("gpt", "sonnet"))}
    world.wave = world.wave.model_copy(update={"months": months, "fixed_remaining_usd": 0.0})
    sub = world.submitter()
    sub.cycle()
    assert jobs_by_name(world.fakes, "gpt")
    assert (
        sub.state.month_fit["gpt"]["decision"] == "run" and sub.state.month_fit["gpt"]["month"] == "2026-12"
    )


def test_deferred_arms_are_dropped_without_a_december_budget_by_the_deadline(tmp_path: Path) -> None:
    world = fit_world(tmp_path, fixed=300, december=DECEMBER)
    world.clock.now = datetime(2026, 11, 21, 9, tzinfo=UTC)
    sub = world.submitter()
    sub.cycle()
    assert sub.state.month_fit["gpt"]["decision"] == "drop"
    assert sub.excluded(world.wave.arm("gpt"))
    assert sub.state.month_fit["sonnet"]["decision"] == "drop"
    assert not jobs_by_name(world.fakes, "gpt") and not jobs_by_name(world.fakes, "sonnet")


def test_sonnet_is_summed_alone_when_gpt_is_not_admitted(tmp_path: Path) -> None:
    world = fit_world(tmp_path, fixed=200, gpt_canary=0.07)
    sub = world.submitter()
    sub.cycle()
    assert not jobs_by_name(world.fakes, "gpt") and jobs_by_name(world.fakes, "sonnet")
    assert "gpt" not in sub.state.month_fit and sub.state.month_fit["sonnet"]["decision"] == "run"


def test_resume_takes_the_decision_again(tmp_path: Path) -> None:
    world = fit_world(tmp_path, fixed=300, december=DECEMBER)
    sub = world.submitter()
    sub.cycle()
    assert sub.state.month_fit["gpt"]["decision"] == "defer"
    world.wave = world.wave.model_copy(update={"fixed_remaining_usd": 0.0})
    sub = world.submitter()
    sub.ledger.command(world.clock.now, "resume", arm="gpt")
    sub.cycle()
    assert sub.state.month_fit["gpt"]["decision"] == "run" and jobs_by_name(world.fakes, "gpt")


def test_sonnet_is_metered_and_throttled_in_its_registry_region(tmp_path: Path) -> None:
    world = fit_world(tmp_path, fixed=100, gpt_canary=0.07)
    sub = world.submitter()
    sonnet = world.wave.arm("sonnet")
    assert sub.region_of(sonnet) == "us-east-2" and sub.region_of(world.wave.arm("claude")) == "us-east-1"
    assert sub.profile_regions()[SONNET_MODEL] == "us-east-2"
    sub.cycle()
    world.clock.advance(minutes=10)
    world.fakes.cloudwatch.sums[("InputTokenCount", EAST2_PROFILE)] = 99e6
    world.fakes.cloudwatch_east2.sums[("InputTokenCount", EAST2_PROFILE)] = 1e6
    world.fakes.cloudwatch_east2.sums[("OutputTokenCount", EAST2_PROFILE)] = 1e5
    spent = sub.arm_spent(sonnet, world.clock.now)
    assert round(spent, 4) == 3.3
    assert round(sub.month_meter(world.clock.now).token_usd, 4) == round(2.2 + 1.1, 4)
    world.fakes.cloudwatch_east2.sums[("InvocationThrottles", EAST2_PROFILE)] = 50
    world.fakes.cloudwatch_east2.sums[("Invocations", EAST2_PROFILE)] = 1000
    sub.cycle()
    assert sub.state.cap_cut["sonnet"] == 5
    assert sub.arm_throttles(sonnet, world.clock.now) == 50


def test_s0_checks_the_registry_region_against_the_tagged_profile(tmp_path: Path) -> None:
    world = fit_world(tmp_path, fixed=100)
    sub = world.submitter()
    run = sub.arm_run(world.wave.arm("sonnet"))
    assert sub.arm_preflight(run) == []
    (tmp_path / "src/lost_in_place/llm/models.yaml").write_text("sonnet:\n  provider: bedrock\n")
    sub = world.submitter()
    failures = sub.arm_preflight(sub.arm_run(world.wave.arm("sonnet")))
    assert failures == [
        f"S-0: models.yaml sonnet calls us-east-1, but the tagged profile for {SONNET_MODEL} is in "
        "us-east-2; the meter would miss its tokens"
    ]
    (tmp_path / "src/lost_in_place/llm/models.yaml").write_text("other: {}\n")
    world.stack = STACK
    sub = world.submitter()
    failures = sub.arm_preflight(sub.arm_run(world.wave.arm("sonnet")))
    assert failures == [
        "S-0: models.yaml has no entry sonnet",
        f"S-0: the stack has no tagged profile for {SONNET_MODEL}",
    ]


def test_month_fit_command_prints_every_month_fit_arm(tmp_path: Path, capsys: Any) -> None:
    world = fit_world(tmp_path / "repo", fixed=200, december={**DECEMBER, "stop_usd": 370}, gpt_canary=0.07)
    cfg = tmp_path / "wave.yaml"
    cfg.write_text(yaml.safe_dump(world.wave.model_dump(mode="json", exclude_none=True)))
    root = ["--config", str(cfg), "--root", str(tmp_path / "repo"), "--dry-run"]
    assert (
        submit_main([*root, "--local-dir", str(tmp_path / "l"), "month-fit"], factory=world.fakes, env={})
        == 0
    )
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["arm"] for r in lines] == ["gpt", "sonnet"]
    assert lines[0]["decision"] == "not run in E5" and "exceeds" in lines[0]["reason"]
    assert lines[1]["decision"] in ("run", "defer") and lines[1]["arm_usd"] > 115
    assert world.fakes.writes == []
