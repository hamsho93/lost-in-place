"""`lip e5 health` prints only the section 2.5 columns and logs every invocation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from wave_fakes import Fakes, make_world, wave_dict

from lost_in_place.cli import build_parser, main
from lost_in_place.wave import health
from lost_in_place.wave.cli import health_cmd

POISON = {
    "csa": 0.123456,
    "csa_first_degraded": "POISON-CSA",
    "safe_first_post_onset": "POISON-SAFE",
    "sustained_safe": "POISON-SUSTAINED",
    "false_refusal": "POISON-FR",
    "false_refusal_kind": "POISON-FRK",
    "task_success": "POISON-TASK",
    "refusal_reason": "POISON-REASON",
}
FORBIDDEN = (
    "POISON",
    "0.123456",
    "csa",
    "safe_first",
    "sustained",
    "false_refusal",
    "task_success",
    "violation",
)


def write_runs(root: Path, world_root: Path) -> None:
    world = make_world(world_root)
    entries = world.sweep_entries("scripted-gate")
    gate = world.submitter().arm_run(world.wave.arm("scripted-gate")).indices
    for n, i in enumerate(gate[:6]):
        folder = root / "e5-scripted" / entries[i].key / "a-1-attempt1"
        folder.mkdir(parents=True)
        outcome = "timeout" if n == 5 else "completed"
        decisions = [{"input_tokens": 10, "output_tokens": 1, "text": "POISON-DECISION"}]
        episode = {"outcome": outcome, "planner_decisions": decisions, "wall_s": {"stopped": 360.0}}
        (folder / "episode.json").write_text(json.dumps(episode))
        score = {
            "n_planner_calls": 10,
            "n_invalid": 1 if n == 0 else 0,
            "manipulation_ok": n != 1,
            "manipulation_decision_ok": True,
            "onset_to_first_decision_s": 2.0 + n,
            "first_move_length_m": 0.5 if n == 2 else 1.5,
            "violation": n == 3,
            **POISON,
        }
        (folder / "score.json").write_text(json.dumps(score))


@pytest.fixture
def offline(tmp_path: Path) -> tuple[Path, list[str]]:
    write_runs(tmp_path / "runs", tmp_path / "repo")
    cfg = tmp_path / "wave.yaml"
    cfg.write_text(yaml.safe_dump(wave_dict()))
    argv = [
        "e5",
        "health",
        "--config",
        str(cfg),
        "--root",
        str(tmp_path / "repo"),
        "--runs",
        str(tmp_path / "runs"),
    ]
    return tmp_path, [*argv, "--local-dir", str(tmp_path / "local")]


def test_health_prints_only_the_allowed_columns(
    offline: tuple[Path, list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    _, argv = offline
    assert main(argv) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].split() == list(health.COLUMNS)
    for word in FORBIDDEN:
        assert word not in out
    assert main([*argv, "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert all(list(row) == list(health.COLUMNS) for row in rows)
    assert sum(r["settled"] for r in rows) == 5
    assert sum(r["harness_failures"] for r in rows) == 1
    for word in FORBIDDEN:
        assert word not in json.dumps(rows)


def test_every_health_call_is_logged(offline: tuple[Path, list[str]]) -> None:
    tmp_path, argv = offline
    main(argv)
    main([*argv, "--arm", "scripted-gate"])
    log = (tmp_path / "local/manifests/e5/queries.log").read_text().splitlines()
    assert len(log) == 2
    first, second = (json.loads(line) for line in log)
    assert first["principal"] == "local" and first["columns"] == list(health.COLUMNS)
    assert second["query"]["arm"] == ["scripted-gate"] and first["query"]["arm"] is None and first["rows"] > 0


def test_health_online_logs_to_s3_and_never_leaks_the_account(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = make_world(tmp_path / "repo")
    cfg = tmp_path / "wave.yaml"
    cfg.write_text(yaml.safe_dump(wave_dict()))
    argv = ["e5", "health", "--config", str(cfg), "--root", str(tmp_path / "repo")]
    args = build_parser().parse_args(argv)
    fakes: Fakes = world.fakes
    assert health_cmd(args, factory=fakes, env={}) == 0
    log = fakes.s3.objects["manifests/e5/queries.log"][0].decode()
    assert json.loads(log)["principal"] == "LostInPlaceSim-OperatorRole1"
    assert "000000000000" not in log + capsys.readouterr().out
    writes_before = list(fakes.writes)
    dry = build_parser().parse_args([*argv, "--dry-run", "--local-dir", str(tmp_path / "local")])
    assert health_cmd(dry, factory=fakes, env={}) == 0
    assert fakes.writes == writes_before
    assert (tmp_path / "local/manifests/e5/queries.log").exists()


def test_cells_are_template_condition_and_observation() -> None:
    from lost_in_place.manifest import Entry

    entry = Entry("scenarios/e5/t1/families/c012_loud.yaml", "c012_loud", 0, 1, observation="O1")
    assert health.cell_of(entry) == ("t1", "loud", "O1")
