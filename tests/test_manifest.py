import json
from pathlib import Path
from typing import Any

import pytest

from lost_in_place import manifest
from lost_in_place.manifest import Entry, SweepSpec

ROOT = Path(__file__).parents[1]


def _spec(**overrides: object) -> SweepSpec:
    data = {
        "name": "t",
        "scenarios": ["scenarios/pilot/cross_*.yaml"],
        "reps": 2,
        "validators": ["none", "geometric"],
    }
    return SweepSpec({**data, **overrides})


def test_the_e1_pilot_sweep_crosses_every_arm() -> None:
    entries = manifest.build(SweepSpec.load(ROOT / "sweeps" / "e1-pilot.yaml"), ROOT)
    assert len(entries) == 9 * 10 * 3
    assert len({e.key for e in entries}) == len(entries)
    assert {e.validator for e in entries} == {"none", "geometric", "estimator_health"}


def test_matched_scenarios_and_arms_share_seeds() -> None:
    entries = manifest.build(_spec(), ROOT)
    assert len(entries) == 3 * 2 * 2
    seeds: dict[int, set[int]] = {}
    for e in entries:
        seeds.setdefault(e.rep, set()).add(e.seed)
    assert all(len(s) == 1 for s in seeds.values())  # healthy, loud, silent and both validators
    assert seeds[0] != seeds[1]
    assert manifest.episode_seed("cross", 0) == next(iter(seeds[0]))


def test_seeds_are_stable_across_runs() -> None:
    assert manifest.episode_seed("cross", 0) == 1638029371


def test_order_is_shuffled_but_reproducible() -> None:
    a, b = manifest.build(_spec(), ROOT), manifest.build(_spec(), ROOT)
    assert a == b
    assert a != sorted(a, key=lambda e: e.key)
    assert manifest.build(_spec(shuffle_seed=1), ROOT) != a


def test_manifest_round_trips_and_records_the_spec() -> None:
    spec = _spec()
    entries = manifest.build(spec, ROOT)
    text = manifest.dumps(spec, entries)
    assert manifest.loads(text) == entries
    assert '"spec_sha256"' in text


def test_episode_key_names_the_cell_and_seed() -> None:
    e = Entry("scenarios/pilot/cross_loud.yaml", "cross_loud", 7, 123, validator="geometric")
    assert e.key == "cross_loud/scripted-naive-geometric-O0/r007-s123"


def test_llm_arms_are_named_by_their_model(tmp_path: Path) -> None:
    models = ROOT / "src" / "lost_in_place" / "llm" / "models.yaml"
    model = next(iter(manifest.yaml.safe_load(models.read_text())))
    entries = manifest.build(_spec(planners=["scripted-naive", model], validators=["none"]), ROOT)
    llm = [e for e in entries if e.planner == "llm"]
    assert len(llm) == len(entries) // 2 and all(e.planner_options == {"model": model} for e in llm)
    assert {e.cell for e in entries} == {"scripted-naive-none-O0", f"{model}-none-O0"}
    assert len({e.key for e in entries}) == len(entries)
    with pytest.raises(LookupError, match="no model 'gpt-nope'"):
        manifest.build(_spec(planners=["gpt-nope"]), ROOT)


def test_unknown_sweep_keys_and_empty_globs_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown"):
        _spec(seeds=3)
    with pytest.raises(ValueError, match="no scenarios"):
        manifest.build(_spec(scenarios=["nope/*.yaml"]), ROOT)


def test_sweep_names_must_be_safe_path_segments() -> None:
    for bad in ("", "E1", "a/b", "../x", "-x"):
        with pytest.raises(ValueError, match="sweep name"):
            _spec(name=bad)


def test_each_sweep_has_its_own_results_prefix() -> None:
    entries = manifest.build(_spec(), ROOT)
    a = json.loads(manifest.dumps(_spec(name="pilot"), entries))
    b = json.loads(manifest.dumps(_spec(name="confirmatory"), entries))
    assert manifest.runs_root("s3://bucket/runs/", a) == "s3://bucket/runs/pilot"
    assert manifest.runs_root("s3://bucket/runs", b) == "s3://bucket/runs/confirmatory"
    del a["runs_prefix"]  # a manifest built before the prefix existed
    assert manifest.runs_root("s3://bucket/runs", a) == "s3://bucket/runs"


def test_run_manifest_skips_only_results_from_its_own_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from lost_in_place import cli, storage

    spec = _spec(name="second")
    path = tmp_path / "second.json"
    path.write_text(manifest.dumps(spec, manifest.build(spec, ROOT)))
    entry = manifest.read(str(path))[0]
    # The same episode, from another sweep.
    earlier: dict[str, list[tuple[str, dict[str, Any] | None]]] = {
        f"s3://bucket/runs/{entry.key}": [("completed", None)]
    }
    mine = {f"s3://bucket/runs/second/{entry.key}": [("completed", None)]}
    seen: list[str] = []

    def results(uri: str, key: str) -> list[tuple[str, dict[str, Any] | None]]:
        seen.append(f"{uri}/{key}")
        return {**earlier, **mine}.get(f"{uri}/{key}", [])

    monkeypatch.setattr(storage, "attempt_results", results)
    assert cli.main(["run-manifest", str(path), "--index", "0", "--upload", "s3://bucket/runs"]) == 0
    assert seen == [f"s3://bucket/runs/second/{entry.key}"]
    assert '"skipped"' in capsys.readouterr().out


def test_arrays_are_split_at_the_batch_limit() -> None:
    assert manifest.array_chunks(25_000) == [(0, 10_000), (10_000, 10_000), (20_000, 5_000)]
    assert manifest.array_chunks(270) == [(0, 270)]


VIOLATED = {"violation": True}
CLEAN = {"violation": False}


@pytest.mark.parametrize(
    ("attempts", "settled"),
    [
        ([], False),
        ([("boot_stall", None)], False),
        ([("boot_stall", None), ("completed", CLEAN)], True),
        ([("error: OSError", None), ("sim_stall: no clock", None), ("timeout", None)], True),
        ([("inject_failed: denied", None), ("pending", None)], False),
        ([("sim_stall: frozen at 41.0 s", VIOLATED)], True),
        ([("timeout", CLEAN)], False),
        ([("error: LinkLost", {"error": "never armed"})], False),
    ],
    ids=[
        "none",
        "boot",
        "then-result",
        "three-failures",
        "two-failures",
        "stall-after-violation",
        "clean-timeout",
        "never-armed",
    ],
)
def test_an_episode_is_settled_by_a_result_or_three_attempts(
    attempts: list[tuple[str, dict[str, Any] | None]], settled: bool
) -> None:
    assert manifest.settled(attempts) is settled


@pytest.mark.parametrize(
    ("outcome", "score", "code"),
    [
        ("completed", {"violation": False}, 0),
        ("sim_stall: frozen at 41.0 s", {"violation": True}, 0),
        ("sim_stall: frozen at 41.0 s", {"violation": False}, 1),
        ("timeout", None, 1),
    ],
)
def test_batch_retries_a_harness_failure_unless_it_came_after_a_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, score: dict[str, Any] | None, code: int
) -> None:
    import contextlib

    from lost_in_place import cli, episode, slots
    from lost_in_place.agent import AgentConfig

    def fly(scenario: Any, seed: int, out: Path, **_: Any) -> Any:
        out.mkdir(parents=True, exist_ok=True)
        (out / "flight.ulg").write_bytes(b"")
        return episode.EpisodeResult(
            scenario_id=scenario.id, seed=seed, instance=0, world="w", spawn={}, outcome=outcome
        )

    def analyze(run_dirs: list[Path]) -> int:
        if score is not None:
            (run_dirs[0] / "score.json").write_text(json.dumps(score))
        return 0

    monkeypatch.setattr(episode, "run_episode", fly)
    monkeypatch.setattr(slots, "claim_slot", lambda instance: contextlib.nullcontext(0))
    monkeypatch.setattr(cli, "_analyze", analyze)
    scenario = ROOT / "scenarios" / "pilot" / "cross_loud.yaml"
    out = tmp_path / "run"
    assert cli._fly(scenario, 0, out, AgentConfig(), instance=None, analyze=True, upload=None) == code
