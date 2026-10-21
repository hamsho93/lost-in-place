"""E5b (`scripts/e5b.py`, SPEC-v3 amendment of 2026-10-21): the sweeps are E5's with only the names
changed, E5's freeze record still describes E5, the wave config pins what the sweeps fly, and the
admission rule runs an arm only on an accepted smoke call and an affordable canary."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from lost_in_place.llm.registry import load_models
from lost_in_place.wave import config
from lost_in_place.wave.aws import mask
from lost_in_place.wave.store import LocalStore

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "e5b.py"
CONFIG = ROOT / "src/lost_in_place/wave/e5b.yaml"


@pytest.fixture(scope="module")
def e5b() -> ModuleType:
    spec = importlib.util.spec_from_file_location("e5b", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_committed_sweeps_are_the_generators(e5b: ModuleType) -> None:
    assert e5b.main(["sweeps", "--check"]) == 0


@pytest.mark.parametrize("arm", ["llama-3-3-70b", "deepseek-v3-2", "mistral-large-3"])
def test_each_llm_arm_flies_e5s_cells_under_its_own_name(e5b: ModuleType, arm: str) -> None:
    ours = yaml.safe_load((ROOT / "sweeps/e5b" / f"{arm}.yaml").read_text())
    theirs = yaml.safe_load((ROOT / "sweeps/e5/claude-sonnet-5-5.yaml").read_text())
    assert ours["name"] == f"e5b-{arm}"
    assert {k: v for k, v in ours.items() if k not in ("name", "cells")} == {
        k: v for k, v in theirs.items() if k not in ("name", "cells")
    }
    assert len(ours["cells"]) == len(theirs["cells"])
    for mine, e5_cell in zip(ours["cells"], theirs["cells"], strict=True):
        assert [p["arm"] for p in mine["planners"]] == [f"dexi/{arm}"]
        assert [p["options"] for p in mine["planners"]] == [p["options"] for p in e5_cell["planners"]]
        assert (mine["scenarios"], mine["observations"]) == (e5_cell["scenarios"], e5_cell["observations"])


def test_the_scripted_gate_sweep_is_e5s_renamed() -> None:
    ours = yaml.safe_load((ROOT / "sweeps/e5b/scripted.yaml").read_text())
    theirs = yaml.safe_load((ROOT / "sweeps/e5/scripted.yaml").read_text())
    assert ours.pop("name") == "e5b-scripted" and theirs.pop("name") == "e5-scripted"
    assert ours == theirs


def test_the_canaries_keep_e5s_names_and_cells(e5b: ModuleType) -> None:
    e5 = yaml.safe_load((ROOT / "sweeps/e5-canary/t1-claude-sonnet-5-5.yaml").read_text())
    for entry in e5b.ENTRIES:
        ours = yaml.safe_load((ROOT / f"sweeps/e5b-canary/t1-{entry}.yaml").read_text())
        assert ours["name"] == f"e5-canary-t1-{entry}"
        assert [c["scenarios"] for c in ours["cells"]] == [c["scenarios"] for c in e5["cells"]]
        assert {p["arm"] for c in ours["cells"] for p in c["planners"]} == {f"dexi/{entry}"}
    assert not set(e5b.ENTRIES) & {p.stem.removeprefix("t1-") for p in (ROOT / "sweeps/e5-canary").glob("*")}


def test_a_fallback_entry_is_flown_under_its_arms_name(e5b: ModuleType) -> None:
    entries = {arm: entries[0] for arm, entries in e5b.ARMS.items()} | {"deepseek-v3-2": "deepseek-v3-2-fb1"}
    sweep = yaml.safe_load(e5b.wave(entries)["deepseek-v3-2.yaml"])
    assert sweep["name"] == "e5b-deepseek-v3-2"
    assert {p["arm"] for c in sweep["cells"] for p in c["planners"]} == {"dexi/deepseek-v3-2-fb1"}
    with pytest.raises(ValueError, match="use one of"):
        e5b.wave({"llama-3-3-70b": "mistral-large-3"})


def test_e5s_record_still_describes_e5(e5b: ModuleType) -> None:
    problems, values = e5b.check()
    assert problems == []
    assert {f"Entry flown, `{arm}`" for arm in e5b.ARMS} <= set(values)
    assert {f"Wire digest, `{e}`" for e in e5b.ENTRIES} <= set(values)


def test_the_e5b_record_is_read_from_its_section_only(e5b: ModuleType) -> None:
    text = "\n".join(
        [
            "| Model registry with the E5b entries | `aaa` |",
            e5b.SECTION,
            "| E5b artifact | Hash |",
            "|---|---|",
            "| Model registry with the E5b entries | `bbb` |",
            "| E5b image digest | recorded at launch |",
            "## Next",
            "| Wave config (`x`) | `ccc` |",
        ]
    )
    assert e5b.recorded_e5b(text) == {"Model registry with the E5b entries": "bbb"}
    assert e5b.recorded_e5b("no section") == {}


def test_every_e5b_record_row_is_one_check_prints(e5b: ModuleType) -> None:
    recorded = e5b.recorded_e5b((ROOT / "SPEC-v3.md").read_text())
    _, values = e5b.check()
    assert "Model registry with the E5b entries" in recorded
    assert {row: values.get(row) for row in recorded} == recorded


def test_the_image_recipe_must_build_on_one_digest(e5b: ModuleType) -> None:
    digest = "sha256:" + "a" * 64
    assert e5b.dockerfile_bases((ROOT / "docker/Dockerfile.e5b").read_text()) == [
        "sha256:5470354eeb560ff84727ba89e69843b9d696dceac8075ebbea50e989bbde5f86"
    ]
    assert e5b.dockerfile_bases(f"FROM ${{R}}@{digest}\nFROM ubuntu:24.04 AS b\n") == [digest, ""]


def test_analyse_refuses_while_blinded(e5b: ModuleType, tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert e5b.analyse("s3://b/runs", tmp_path / "out", root=tmp_path) == 3
    assert not (tmp_path / "out.json").exists()


def test_canary_health_logs_its_read(e5b: ModuleType, tmp_path: Path) -> None:
    result = {"template": "t1", "arms": {"llama-3-3-70b": _canary()}}
    where = e5b.log_canary_health(
        LocalStore(tmp_path), "local", {"cmd": "canary-health", "runs_root": "runs"}, result
    )
    (line,) = (tmp_path / "manifests/e5b/queries.log").read_text().splitlines()
    logged = json.loads(line)
    assert where == str(tmp_path / "manifests/e5b/queries.log")
    assert logged["rows"] == 1 and logged["query"]["cmd"] == "canary-health"
    assert logged["columns"] == sorted(_canary())


def test_the_frozen_registry_must_be_a_prefix(e5b: ModuleType) -> None:
    frozen = b"a:\n  x: 1\n"
    recorded = e5b._sha(frozen)
    assert e5b.frozen_registry_length(frozen + b"b:\n  y: 2\n", recorded) == len(frozen)
    assert e5b.frozen_registry_length(b"a:\n  x: 2\nb:\n  y: 2\n", recorded) is None


def test_a_config_naming_another_entry_than_the_sweep_fails(e5b: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "sweeps/e5b").mkdir(parents=True)
    for name in ("llama-3-3-70b", "deepseek-v3-2", "mistral-large-3"):
        (tmp_path / "sweeps/e5b" / f"{name}.yaml").write_text(
            (ROOT / "sweeps/e5b" / f"{name}.yaml").read_text()
        )
    data = yaml.safe_load(CONFIG.read_text())
    (tmp_path / "src/lost_in_place/wave").mkdir(parents=True)
    (tmp_path / "src/lost_in_place/wave/e5b.yaml").write_text(yaml.safe_dump(data))
    assert e5b.flown_entries(tmp_path) == ([], {arm: entries[0] for arm, entries in e5b.ARMS.items()})
    next(a for a in data["arms"] if a["name"] == "deepseek-v3-2")["model"] = "deepseek-v3-2-fb1"
    (tmp_path / "src/lost_in_place/wave/e5b.yaml").write_text(yaml.safe_dump(data))
    problems, _ = e5b.flown_entries(tmp_path)
    assert len(problems) == 1 and "deepseek-v3-2" in problems[0]


def test_the_wave_config_pins_the_registry_and_sends_nothing_yet(e5b: ModuleType) -> None:
    wave = config.load(CONFIG)
    registry = yaml.safe_load((ROOT / "src/lost_in_place/llm/models.yaml").read_text())
    models = load_models()
    llm = [a for a in wave.arms if a.kind == "batch" and a.name != "scripted-gate"]
    assert [a.name for a in llm] == list(e5b.ARMS) == [a.name for a in wave.month_fit_arms()]
    for arm in llm:
        assert arm.pinned_model is not None and arm.model in e5b.ARMS[arm.name]
        assert {k: registry[arm.model].get(k) for k in arm.pinned_model} == arm.pinned_model
        assert arm.bedrock_model == models[arm.model].model_id and models[arm.model].region == "us-east-2"
        assert arm.needs_gate and arm.canary_usd_per_episode is None
        probe = wave.arm(f"probe-{arm.name}")
        assert probe.counted_in == arm.name and probe.estimate_usd == arm.probe_usd
        assert probe.price == arm.price and probe.bedrock_model == arm.bedrock_model
    assert all(m.stop_usd is None for m in wave.months.values())
    assert wave.preflight_commands == (("uv", "run", "python", "scripts/e5b.py", "check"),)
    assert sum(a.cap for a in wave.arms if a.name in e5b.ARMS) <= wave.max_in_flight
    text = CONFIG.read_text()
    assert mask(text) == text and "s3://{bucket}/" in text


def _canary(per_episode: float = 0.011, invalid: int = 0, loud: int = 42, brief: int = 36) -> dict[str, Any]:
    return {
        "episodes": 78,
        "planner_calls": 350,
        "invalid_calls": invalid,
        "loud_episodes": 42,
        "loud_manipulation_decision_ok": loud,
        "brief_episodes": 36,
        "brief_manipulation_ok": brief,
        "usd": per_episode * 78,
    }


def _accepted(*rejected: str, entry: str) -> dict[str, Any]:
    tried = [{"entry": e, "result": "rejected", "error": "ValidationException"} for e in rejected]
    return {"tried": [*tried, {"entry": entry, "result": "accepted"}]}


def test_admission_runs_every_arm_descriptive(e5b: ModuleType) -> None:
    smoke = {
        "llama-3-3-70b": _accepted(entry="llama-3-3-70b"),
        "deepseek-v3-2": _accepted("deepseek-v3-2", entry="deepseek-v3-2-fb1"),
        "mistral-large-3": _accepted(entry="mistral-large-3"),
    }
    canary = {
        "arms": {
            "llama-3-3-70b": _canary(invalid=20),
            "deepseek-v3-2-fb1": _canary(loud=30),
            "mistral-large-3": _canary(),
        }
    }
    out = e5b.admission(smoke, canary)
    assert {arm: (r["entry"], r["status"]) for arm, r in out.items()} == {
        "llama-3-3-70b": ("llama-3-3-70b", "descriptive"),
        "deepseek-v3-2": ("deepseek-v3-2-fb1", "descriptive"),
        "mistral-large-3": ("mistral-large-3", "descriptive"),
    }
    assert not out["llama-3-3-70b"]["checks"]["2_invalid"]["ok"]
    assert not out["deepseek-v3-2"]["checks"]["3_manipulation"]["ok"]
    assert out["mistral-large-3"]["checks"]["5_retest"] == {"ok": True, "required": False}


def test_admission_does_not_run_an_arm_without_a_smoke_call_or_over_the_cost_limit(e5b: ModuleType) -> None:
    rejected = {"tried": [{"entry": e, "result": "rejected"} for e in e5b.ARMS["mistral-large-3"]]}
    smoke = {"llama-3-3-70b": _accepted(entry="llama-3-3-70b"), "mistral-large-3": rejected}
    out = e5b.admission(smoke, {"arms": {"llama-3-3-70b": _canary(per_episode=0.061)}})
    assert {arm: r["status"] for arm, r in out.items()} == dict.fromkeys(e5b.ARMS, "not run")
    assert not out["llama-3-3-70b"]["checks"]["4_cost"]["ok"]
    with pytest.raises(SystemExit, match="unknown arms"):
        e5b.admission({"gpt-5.6-sol": {}}, {"arms": {}})


def test_a_fallback_tried_before_its_base_entry_is_refused(e5b: ModuleType) -> None:
    smoke = {"deepseek-v3-2": _accepted(entry="deepseek-v3-2-fb1")}
    out = e5b.admission(smoke, {"arms": {"deepseek-v3-2-fb1": _canary()}})
    assert (
        out["deepseek-v3-2"]["status"] == "not run"
        and out["deepseek-v3-2"]["checks"]["1_smoke_call"]["problems"]
    )
