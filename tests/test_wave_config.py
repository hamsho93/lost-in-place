"""The E5 wave config, stack discovery, read-only clients and the ledger stores."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from wave_fakes import BUCKET, ClientError, FakeCFN, Fakes, FakeS3, make_world, wave_dict

from lost_in_place.wave import config
from lost_in_place.wave.aws import Clients, WriteRefused, discover, mask
from lost_in_place.wave.cli import submit_main
from lost_in_place.wave.store import LocalStore, Overlay, S3Store

ROOT = Path(__file__).resolve().parents[1]


def test_the_e5_wave_loads_with_the_planned_caps_and_order() -> None:
    wave = config.load()
    caps = {a.name: a.cap for a in wave.arms if a.kind == "batch"}
    assert caps == {
        "scripted-gate": 64,
        "claude": 25,
        "student": 39,
        "zero-shot": 39,
        "scripted-rest": 64,
        "gpt-6.1-sol": 40,
        "claude-sonnet-5-5": 40,
    }
    zero = wave.arm("zero-shot")
    assert [(r.after.arm, r.cap) for r in zero.cap_rules] == [("claude", 64)]
    assert wave.months["2026-10"].stop_usd == 420 and wave.months["2026-11"].stop_usd == 400
    assert wave.ledger_prefix == "manifests/e5/ledger" and wave.queries_log == "manifests/e5/queries.log"
    assert {a.name for a in wave.outside_month_fit()} == {a.name for a in wave.arms} - {
        "gpt-6.1-sol",
        "probe-gpt",
        "claude-sonnet-5-5",
        "probe-sonnet-5-5",
    }
    assert [a.name for a in wave.month_fit_arms()] == ["gpt-6.1-sol", "claude-sonnet-5-5"]
    gpt, sonnet = wave.arm("gpt-6.1-sol"), wave.arm("claude-sonnet-5-5")
    assert gpt.month_fit and gpt.canary_usd_per_episode is None
    assert sonnet.canary_usd_per_episode is None and {c.arm for c in sonnet.start_after} >= {"gpt-6.1-sol"}
    assert sonnet.price is not None and (sonnet.price.input_per_mtok, sonnet.price.output_per_mtok) == (
        2.2,
        11,
    )
    assert sonnet.pinned_model is not None and sonnet.pinned_model["region"] == "us-east-2"
    december = wave.months["2026-12"]
    assert december.stop_usd is None and set(december.arms or ()) == {
        "gpt-6.1-sol",
        "probe-gpt",
        "claude-sonnet-5-5",
        "probe-sonnet-5-5",
    }
    assert all(a.needs_gate for a in wave.arms if a.kind == "batch" and not a.name.startswith("scripted"))


def test_the_config_names_no_account_or_bucket() -> None:
    text = (ROOT / "src/lost_in_place/wave/e5.yaml").read_text()
    assert mask(text) == text and "s3://{bucket}/" in text


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["arms"].append(dict(d["arms"][0])), "duplicate"),
        (lambda d: d["arms"][1].update(start_after=[{"arm": "nope"}]), "unknown arm"),
        (lambda d: d["arms"].append({"name": "x", "kind": "external", "sweep": "s.yaml"}), "external"),
        (lambda d: d["arms"][1].pop("price"), "needs a price"),
        (lambda d: d.update(months={"Nov": {"stop_usd": 1}}), "YYYY-MM"),
    ],
)
def test_config_validation(change: Any, message: str) -> None:
    data = wave_dict()
    change(data)
    with pytest.raises(ValueError, match=message):
        config.Wave.model_validate(data)


def test_discover_reads_outputs_and_env_overrides() -> None:
    stack = discover(FakeCFN(), "LostInPlaceSim", "SweepJobDefinition", {})
    assert stack.bucket == BUCKET and stack.profile_id("us.test.model") == "prof1"
    assert stack.operator_role_name == "LostInPlaceSim-OperatorRole1" and stack.job_role_name == "Lip-JobRole"
    other = discover(FakeCFN(), "LostInPlaceSim", "SweepJobDefinition", {"LIP_JOB_QUEUE": "q2"})
    assert other.queue == "q2"


def test_read_only_clients_refuse_writes() -> None:
    clients = Clients(Fakes(), read_only=True)
    clients("s3").get_paginator("list_objects_v2")
    with pytest.raises(WriteRefused):
        clients("s3").put_object(Bucket="b", Key="k", Body=b"")
    with pytest.raises(WriteRefused):
        clients("batch").submit_job(jobName="x")
    with pytest.raises(WriteRefused):
        clients("bedrock").delete_imported_model(modelIdentifier="x")


def test_s3_append_retries_on_a_conflicting_writer() -> None:
    s3 = FakeS3([])
    store = S3Store(s3, BUCKET, sleep=lambda _: None)
    store.append("log", "a\n")
    real_put = s3.put_object
    raced = []

    def put(**kwargs: Any) -> None:
        if not raced:
            raced.append(1)
            s3.objects["log"] = (s3.objects["log"][0] + b"other\n", s3.clock())
            raise ClientError("PreconditionFailed")
        real_put(**kwargs)

    s3.put_object = put  # type: ignore[method-assign]
    store.append("log", "b\n")
    assert store.get("log") == b"a\nother\nb\n"


def test_overlay_reads_through_and_writes_locally(tmp_path: Path) -> None:
    s3 = FakeS3([])
    s3.put("ledger/s4.log", b"x\n")
    store = Overlay(S3Store(s3, BUCKET), LocalStore(tmp_path))
    store.append("ledger/s4.log", "y\n")
    store.put("ledger/new", b"1")
    assert store.get("ledger/s4.log") == b"x\ny\n" and s3.objects["ledger/s4.log"][0] == b"x\n"
    assert store.keys("ledger/") == ["ledger/new", "ledger/s4.log"]
    assert s3.writes == []


def test_submit_cli_dry_run_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    world = make_world(tmp_path / "repo")
    cfg = tmp_path / "wave.yaml"
    cfg.write_text(yaml.safe_dump(wave_dict()))
    common = [
        "--config",
        str(cfg),
        "--root",
        str(tmp_path / "repo"),
        "--dry-run",
        "--local-dir",
        str(tmp_path / "l"),
    ]
    assert submit_main([*common, "preflight"], factory=world.fakes, env={}) == 0
    assert submit_main([*common, "run", "--once"], factory=world.fakes, env={}) == 0
    assert submit_main([*common, "pause", "claude", "--reason", "check"], factory=world.fakes, env={}) == 0
    assert submit_main([*common, "window", "labeller", "start"], factory=world.fakes, env={}) == 2
    assert submit_main([*common, "status"], factory=world.fakes, env={}) == 0
    assert world.fakes.writes == []
    assert (tmp_path / "l/manifests/e5/ledger/commands.log").read_text().count("pause") == 1
    out = capsys.readouterr().out
    assert "000000000000" not in out and "pre-flight passed" in out
