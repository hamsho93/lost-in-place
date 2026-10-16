import shutil
import subprocess
from pathlib import Path

import pytest

from lost_in_place import cli, freeze

ROOT = Path(__file__).resolve().parents[1]
SPEC_V3_TEXT = (ROOT / freeze.SPEC_V3).read_text()
ARMS = (
    "dexi-qwen3-4b-zero-shot.driver-dexi.prompt-dexi-v2",
    "dexi-claude-sonnet-4-6.driver-dexi.prompt-dexi-v2",
    "dexi-qwen3-4b-student.driver-dexi.prompt-dexi-v2",
)
ADAPTATION = freeze.Adaptation("t1", 120, ARMS)
DIGEST = "sha256:" + "0" * 64
VALUES = {row: f"{i:064x}" for i, row in enumerate(freeze.V3_ROWS)} | ADAPTATION.values()


def _table(spec_text: str) -> list[tuple[str, str]]:
    lines = spec_text.split("\n")
    start = lines.index(freeze.RECORD_HEADING)
    end = lines.index("## Amendment log")
    rows = [(m["artifact"], m["value"]) for line in lines[start:end] if (m := freeze._ROW.match(line))]
    return [row for row in rows if row[0] != "Artifact"]


GIT_CONFIG = (
    "-c",
    "user.name=t",
    "-c",
    "user.email=t@example.com",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.fsmonitor=false",
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *GIT_CONFIG, *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout with every artifact the v3 record hashes, and a probe text read from a script."""
    root = tmp_path / "repo"
    files = {
        freeze.DESIGN_V3: "design\n",
        freeze.MODEL_REGISTRY: (ROOT / freeze.MODEL_REGISTRY).read_text(),
        freeze.ANALYSIS_SCRIPT_V3: "stats\n",
        freeze.ADMISSION_SCRIPT: "admission\n",
        "scripts/probe_text.py": 'TEXT = "Where is the point?"\n',
        f"{freeze.E5_SWEEPS}/e5-claude.yaml": "sweep\n",
        **{
            f"{freeze.E5_SCENARIOS}/{t}/{name}/{t}_{name}.yaml": f"{t} {name}\n"
            for t in freeze.E5_TEMPLATES
            for name in freeze.E5_SETS
        },
    }
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    shutil.copy(ROOT / freeze.SPEC_V3, root / freeze.SPEC_V3)
    monkeypatch.setattr(freeze, "PROBE_TEXTS", {"wording-check": "scripts/probe_text.py:TEXT"})
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "pre")
    return root


def test_the_freeze_type_names_each_record() -> None:
    assert freeze.Freeze("v2") is freeze.Freeze.V2 and freeze.Freeze.V2 == "v2"
    assert (freeze.Freeze.V2.spec, freeze.Freeze.V2.rows) == (freeze.SPEC, freeze.ALL_ROWS)
    assert (freeze.Freeze.V3.spec, freeze.Freeze.V3.rows) == (freeze.SPEC_V3, freeze.V3_ROWS)


def test_spec_v3_has_every_v3_row_in_order_and_blank_until_the_freeze() -> None:
    assert _table(SPEC_V3_TEXT) == [(row, "") for row in freeze.V3_ROWS]


def test_the_rows_stats_v3_reads_keep_their_exact_strings() -> None:
    assert freeze.ANALYSIS_ROW_V3 == "Analysis script (`src/lost_in_place/stats_v3.py`)"
    assert (freeze.ARMS_ROW, freeze.M_ROW, freeze.N_ROW) == (
        "Confirmatory arms",
        "Wave-wide m",
        "Families (N)",
    )
    assert (freeze.SPEC_V3, freeze.RECORD_HEADING) == ("SPEC-v3.md", "## Freeze record")


def test_the_record_parses_the_same_in_stats_v3() -> None:
    stats_v3 = pytest.importorskip("lost_in_place.stats_v3")
    assert (stats_v3.ANALYSIS_ROW, stats_v3.ARMS_ROW, stats_v3.M_ROW, stats_v3.N_ROW) == (
        freeze.ANALYSIS_ROW_V3,
        freeze.ARMS_ROW,
        freeze.M_ROW,
        freeze.N_ROW,
    )
    assert (stats_v3.SPEC_V3, stats_v3.RECORD_HEADING, stats_v3.SCRIPT) == (
        freeze.SPEC_V3,
        freeze.RECORD_HEADING,
        freeze.ANALYSIS_SCRIPT_V3,
    )
    written = freeze.write_record_v3(SPEC_V3_TEXT, VALUES)
    design = stats_v3.read_design(written)
    assert (design.confirmatory, design.m, design.n) == (tuple(sorted(ARMS)), 12, 120)
    assert stats_v3.record_rows(written)[freeze.ANALYSIS_ROW_V3] == VALUES[freeze.ANALYSIS_ROW_V3]


def test_writing_the_v3_record_does_not_change_its_hash() -> None:
    written = freeze.write_record_v3(SPEC_V3_TEXT, VALUES)
    assert written != SPEC_V3_TEXT
    assert freeze.spec_hash_v3(written) == freeze.spec_hash_v3(SPEC_V3_TEXT)
    assert freeze.recorded_v3(written) == VALUES
    assert freeze.write_record_v3(written, VALUES) == written


def test_any_other_spec_v3_edit_changes_its_hash() -> None:
    edited = SPEC_V3_TEXT.replace("Tango", "Tango ", 1)
    assert freeze.spec_hash_v3(edited) != freeze.spec_hash_v3(SPEC_V3_TEXT)


def test_a_record_table_without_a_v3_row_is_refused() -> None:
    broken = SPEC_V3_TEXT.replace(f"| {freeze.M_ROW} | |\n", "")
    with pytest.raises(ValueError, match="no row for Wave-wide m"):
        freeze.write_record_v3(broken, VALUES)


def test_an_adaptation_fixes_m_and_rejects_values_outside_the_rules() -> None:
    assert ADAPTATION.m == 12 and freeze.Adaptation("t0", 160, ARMS[:1]).m == 4
    assert ADAPTATION.values()[freeze.ARMS_ROW] == ", ".join(sorted(ARMS))
    for template, n, arms in [
        ("t2", 120, ARMS),
        ("t0", 140, ARMS),
        ("t0", 120, ()),
        ("t0", 120, ARMS[:1] * 2),
    ]:
        with pytest.raises(ValueError):
            freeze.Adaptation(template, n, arms)
    assert freeze.parse_arms("b, `a`,, c") == ("a", "b", "c")


def test_the_adaptation_is_read_back_from_the_record() -> None:
    written = freeze.write_record_v3(SPEC_V3_TEXT, VALUES)
    assert freeze.adaptation_from_record(written) == freeze.Adaptation("t1", 120, tuple(sorted(ARMS)))
    with pytest.raises(ValueError, match="no value for"):
        freeze.adaptation_from_record(SPEC_V3_TEXT)
    with pytest.raises(ValueError, match="4 x 3"):
        freeze.adaptation_from_record(freeze.write_record_v3(SPEC_V3_TEXT, VALUES | {freeze.M_ROW: "16"}))


def test_record_v3_hashes_the_chosen_templates_directories(repo: Path) -> None:
    values, missing = freeze.record_v3(repo, DIGEST, ADAPTATION)
    assert missing == {}
    for name, row in freeze.SCENARIO_ROWS_V3.items():
        assert values[row] == freeze.sha256(f"t1 {name}\n".encode())
    assert values[freeze.DESIGN_ROW_V3] == freeze.sha256(b"design\n")
    assert values[freeze.SWEEPS_ROW] == freeze.sha256(b"sweep\n")
    assert values[freeze.ANALYSIS_ROW_V3] == freeze.sha256(b"stats\n")
    assert values[freeze.PROMPT_ROW_V3] == freeze.prompt_hash_v3()
    assert values[freeze.PROBE_ROW_V3] == freeze.sha256(
        freeze._canonical({"wording-check": "Where is the point?"})
    )
    assert values[freeze.LABELLING_ROW] == freeze.labelling_hash()
    assert values[freeze.IMAGE_ROW] == DIGEST and len(values[freeze.GIT_ROW]) == 40
    assert values[freeze.ROWS[0]] == freeze.spec_hash_v3(SPEC_V3_TEXT)
    assert [values[row] for row in (freeze.TEMPLATE_ROW, freeze.N_ROW, freeze.M_ROW)] == ["t1", "120", "12"]
    t0, _ = freeze.record_v3(repo, DIGEST, freeze.Adaptation("t0", 120, ARMS))
    assert {row for row in freeze.V3_ROWS if t0[row] != values[row]} == {
        freeze.TEMPLATE_ROW,
        *freeze.SCENARIO_ROWS_V3.values(),
    }
    bare, _ = freeze.record_v3(repo, None, None)
    assert all(bare[row] == "" for row in (*freeze.SCENARIO_ROWS_V3.values(), freeze.IMAGE_ROW, freeze.M_ROW))


def test_record_v3_names_what_is_missing(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shutil.rmtree(repo / freeze.E5_SWEEPS)
    (repo / freeze.ANALYSIS_SCRIPT_V3).unlink()
    monkeypatch.setattr(freeze, "PROBE_TEXTS", {"later": "lost_in_place.not_yet:TEXT"})
    values, missing = freeze.record_v3(repo, DIGEST, ADAPTATION)
    assert set(missing) == {freeze.SWEEPS_ROW, freeze.ANALYSIS_ROW_V3, freeze.PROBE_ROW_V3}
    assert "no *.yaml files in" in missing[freeze.SWEEPS_ROW]
    assert "no module lost_in_place.not_yet" in missing[freeze.PROBE_ROW_V3]
    assert all(values[row] == "" for row in missing)


def test_probe_texts_are_read_from_a_module_or_a_script(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lost_in_place.llm import dexi

    monkeypatch.setattr(
        freeze, "PROBE_TEXTS", {"a": "lost_in_place.llm.dexi:O1R_RULE", "b": "scripts/probe_text.py:TEXT"}
    )
    assert freeze.probe_bundle_v3(repo) == {"a": dexi.O1R_RULE, "b": "Where is the point?"}
    before = freeze.probe_hash_v3(repo)
    (repo / "scripts/probe_text.py").write_text('TEXT = "Where is the point? Say it."\n')
    assert freeze.probe_hash_v3(repo) != before
    monkeypatch.setattr(freeze, "PROBE_TEXTS", {"c": "scripts/probe_text.py:OTHER"})
    with pytest.raises(freeze.MissingArtifact, match="has no OTHER"):
        freeze.probe_bundle_v3(repo)


def test_cli_writes_then_checks_the_v3_record(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["freeze-record", "--freeze", "v3", "--root", str(repo)]
    outcomes = ["--template", "t1", "--n", "120", "--arms", ",".join(ARMS)]
    assert cli.main([*args, "--image-digest", DIGEST, "--write"]) == 2
    assert cli.main([*args, *outcomes, "--write"]) == 2
    assert cli.main([*args, "--check"]) == 1
    assert "no value for" in capsys.readouterr().err
    assert cli.main([*args, *outcomes, "--image-digest", DIGEST, "--write"]) == 0
    capsys.readouterr()
    spec = (repo / freeze.SPEC_V3).read_text()
    assert freeze.adaptation_from_record(spec) == freeze.Adaptation("t1", 120, tuple(sorted(ARMS)))
    assert cli.main([*args, "--image-digest", DIGEST, "--check"]) == 0
    assert cli.main([*args, "--check"]) == 0
    assert "Image digest not compared" in capsys.readouterr().err
    assert cli.main([*args, "--image-digest", "sha256:" + "1" * 64, "--check"]) == 1
    assert "Image digest: recorded" in capsys.readouterr().err
    (repo / freeze.E5_SCENARIOS / "t0" / "canary" / "t0_canary.yaml").write_text("other\n")
    assert cli.main([*args, "--image-digest", DIGEST, "--check"]) == 0
    (repo / freeze.E5_SCENARIOS / "t1" / "canary" / "t1_canary.yaml").write_text("other\n")
    assert cli.main([*args, "--image-digest", DIGEST, "--check"]) == 1
    assert freeze.SCENARIO_ROWS_V3["canary"] in capsys.readouterr().err
    (repo / freeze.ANALYSIS_SCRIPT_V3).unlink()
    assert cli.main([*args, "--image-digest", DIGEST, "--check"]) == 1
    assert f"{freeze.ANALYSIS_ROW_V3}: missing" in capsys.readouterr().err


def test_cli_write_refuses_a_dirty_checkout_or_a_missing_artifact(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["freeze-record", "--freeze", "v3", "--root", str(repo), "--image-digest", DIGEST, "--write"]
    outcomes = ["--template", "t0", "--n", "160", "--arms", ARMS[0]]
    (repo / freeze.DESIGN_V3).write_text("edited\n")
    assert cli.main([*args, *outcomes]) == 1
    assert "needs a clean checkout" in capsys.readouterr().err
    _git(repo, "commit", "-qam", "design")
    shutil.rmtree(repo / freeze.E5_SWEEPS)
    assert cli.main([*args, *outcomes]) == 1
    assert f"{freeze.SWEEPS_ROW}: missing" in capsys.readouterr().err
    assert (repo / freeze.SPEC_V3).read_text() == SPEC_V3_TEXT
    assert cli.main([*args, "--template", "t0", "--n", "160"]) == 2


def test_cli_prints_the_tag_message_and_names_what_is_missing(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["freeze-record", "--freeze", "v3", "--root", str(repo), "--tag-message"]
    assert cli.main([*args, "--image-digest", DIGEST]) == 0
    out = capsys.readouterr().out
    expected = [
        freeze.sha256(b"design\n"),
        freeze.spec_hash_v3(SPEC_V3_TEXT),
        freeze.prompt_hash_v3(),
        freeze.probe_hash_v3(repo),
        freeze.sha256((repo / freeze.MODEL_REGISTRY).read_bytes()),
        freeze.sha256(b"admission\n"),
        DIGEST,
        *(freeze.sha256(f"{t} {name}\n".encode()) for t in freeze.E5_TEMPLATES for name in freeze.E5_SETS),
    ]
    assert all(f"`{value}`" in out for value in expected)
    assert len([line for line in out.split("\n") if line.startswith("- ")]) == len(expected)
    (repo / freeze.ADMISSION_SCRIPT).unlink()
    assert cli.main(args) == 1
    captured = capsys.readouterr()
    assert "missing: Admission script (`scripts/e5_admission.py`)" in captured.err
    assert "missing: Image digest" in captured.err
    assert freeze.prompt_hash_v3() in captured.out and "admission" not in captured.out.lower()


def test_freeze_record_defaults_to_v2_and_refuses_v3_only_flags(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["freeze-record", "--root", str(ROOT), "--check"]) == 0
    assert cli.main(["freeze-record", "--root", str(ROOT), "--freeze", "v2", "--check"]) == 0
    assert cli.main(["freeze-record", "--root", str(ROOT), "--tag-message"]) == 2
    assert cli.main(["freeze-record", "--root", str(ROOT), "--template", "t1"]) == 2
    assert cli.main(["freeze-record", "--root", str(ROOT), "--freeze", "v3", "--analysis"]) == 2
    assert "only with --freeze v3" in capsys.readouterr().err
