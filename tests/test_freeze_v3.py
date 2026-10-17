import shutil
import subprocess
from pathlib import Path

import pytest

from lost_in_place import cli, freeze, prompt_grid

ROOT = Path(__file__).resolve().parents[1]
SPEC_V3_TEXT = (ROOT / freeze.SPEC_V3).read_text()
ARMS = ("qwen3-4b-zero-shot", "claude-sonnet-4-6", "qwen3-4b-student")
FIVE = (*ARMS, "gpt-5.6-sol-fb1", "claude-sonnet-5-5")
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
    """A checkout with every artifact the v3 record hashes, and a probe text read from a script. The
    render grid needs the committed E5 families, so it is stubbed here; `test_prompt_grid` covers it."""
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
    monkeypatch.setattr(prompt_grid, "render_grid", lambda root: {"sha256": "grid"})
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
    assert design.dropped == () and VALUES[freeze.DROPPED_ROW] == "none"


def test_a_five_arm_record_with_fallbacks_and_a_dropped_arm_reads_the_same_in_stats_v3() -> None:
    stats_v3 = pytest.importorskip("lost_in_place.stats_v3")
    assert (stats_v3.DROPPED_ROW, stats_v3.NO_ARMS) == (freeze.DROPPED_ROW, freeze.NO_ARMS)
    assert stats_v3.PRIMARIES_PER_ARM == freeze.PRIMARIES_PER_ARM
    for entry in (*stats_v3.GPT_ENTRIES, *stats_v3.SONNET_ENTRIES, *ARMS):
        assert freeze.base_arm(entry) == stats_v3.base_arm(entry)
    assert set(stats_v3.SMOKE_ONLY) <= set(freeze.SMOKE_ONLY)
    assert {*stats_v3.SCRIPTED, stats_v3.CAUTIOUS} <= set(freeze.SCRIPTED_ARMS)
    adaptation = freeze.Adaptation("t0", 160, FIVE).drop(("claude-sonnet-5-5",))
    design = stats_v3.read_design(freeze.write_record_v3(SPEC_V3_TEXT, VALUES | adaptation.values()))
    assert (design.m, design.n, design.dropped) == (20, 160, ("claude-sonnet-5-5",))
    assert design.confirmatory == tuple(sorted({*ARMS, "gpt-5.6-sol", "claude-sonnet-5-5"}))


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
    assert freeze.Adaptation("t0", 160, FIVE).m == 20
    assert ADAPTATION.values()[freeze.ARMS_ROW] == ", ".join(sorted(ARMS))
    for template, n, arms, match in [
        ("t2", 120, ARMS, "template"),
        ("t0", 140, ARMS, "N must be"),
        ("t0", 120, (), "1 to 5"),
        ("t0", 120, (*FIVE, "other-model"), "1 to 5 confirmatory arms, got 6"),
        ("t0", 120, ARMS[:1] * 2, "named twice"),
        ("t0", 120, ("claude-sonnet-5-5", "claude-sonnet-5-5-fb2"), "named twice: claude-sonnet-5-5"),
        ("t0", 120, ("gpt-5.6-sol", "gpt-5.6-sol-fb1"), "named twice: gpt-5.6-sol"),
        ("t0", 120, ("dexi-claude-sonnet-5-5.driver-dexi.prompt-dexi-v2",), "not an arm key"),
        ("t0", 120, ("claude-haiku-5-5",), "smoke-only"),
        ("t0", 120, ("rule-2m",), "scripted control"),
        ("t0", 120, ("Claude Sonnet",), "not a registry model name"),
    ]:
        with pytest.raises(ValueError, match=match):
            freeze.Adaptation(template, n, arms)
    assert freeze.parse_arms("b, `a`,, c") == ("a", "b", "c") and freeze.parse_arms("`none`") == ()
    assert (
        freeze.base_arm("claude-sonnet-5-5-fb2")
        == "claude-sonnet-5-5"
        == freeze.base_arm("claude-sonnet-5-5")
    )


def test_a_dropped_arm_stays_in_m_and_must_be_a_confirmatory_arm() -> None:
    five = freeze.Adaptation("t1", 120, FIVE)
    dropped = five.drop(("gpt-5.6-sol",)).drop(("claude-sonnet-5-5",))
    assert dropped.m == 20 and dropped.dropped == ("claude-sonnet-5-5", "gpt-5.6-sol")
    assert dropped.values()[freeze.DROPPED_ROW] == "claude-sonnet-5-5, gpt-5.6-sol"
    assert five.values()[freeze.DROPPED_ROW] == "none"
    for arms in [("qwen3-4b-teacher",), ("gpt-5.6-sol", "gpt-5.6-sol-fb1")]:
        with pytest.raises(ValueError, match="dropped arms must be confirmatory arms, once each"):
            five.drop(arms)


def test_the_adaptation_is_read_back_from_the_record() -> None:
    written = freeze.write_record_v3(SPEC_V3_TEXT, VALUES)
    assert freeze.adaptation_from_record(written) == freeze.Adaptation("t1", 120, tuple(sorted(ARMS)))
    dropped = freeze.Adaptation("t1", 120, FIVE).drop(("gpt-5.6-sol",))
    assert freeze.adaptation_from_record(freeze.write_record_v3(SPEC_V3_TEXT, VALUES | dropped.values())) == (
        freeze.Adaptation("t1", 120, tuple(sorted(FIVE)), ("gpt-5.6-sol",))
    )
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
    assert (
        values[freeze.SECOND_LABELLING_ROW] == freeze.second_labelling_hash() != values[freeze.LABELLING_ROW]
    )
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


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SECOND_LABELLER_MODEL", "other-model"),
        ("DOUBLE_CODED_ARM", "claude-sonnet-5-5"),
        ("MATCHED_SEED", 2),
        ("FLAG_POINTS", 0.1),
        ("BRIEF", "x"),
    ],
)
def test_the_second_labelling_hash_follows_its_bundle_and_leaves_freeze_v2s_alone(
    monkeypatch: pytest.MonkeyPatch, name: str, value: object
) -> None:
    from lost_in_place import labels

    second, first = freeze.second_labelling_hash(), freeze.labelling_hash()
    monkeypatch.setattr(labels, name, value)
    assert freeze.second_labelling_hash() != second
    assert (freeze.labelling_hash() == first) is (name != "BRIEF")


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


FIFTH_ARM_ENTRIES = {
    "gpt-5.6-sol-fb1": "  provider: bedrock\n  model_id: us.openai.gpt-5.6-sol\n  region: us-east-2\n",
    "claude-sonnet-5-5": (
        "  provider: bedrock\n  model_id: us.anthropic.claude-sonnet-5-5\n  region: us-east-2\n"
    ),
}


def test_cli_records_five_arms_then_drops_one(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["freeze-record", "--freeze", "v3", "--root", str(repo)]
    write = [*args, "--template", "t0", "--n", "160", "--arms", ",".join(FIVE), "--image-digest", DIGEST]
    unknown = [*args, "--template", "t0", "--n", "160", "--arms", ",".join((*ARMS, "claude-sonnet-9"))]
    assert cli.main([*unknown, "--image-digest", DIGEST, "--write"]) == 1
    assert "not in src/lost_in_place/llm/models.yaml: claude-sonnet-9" in capsys.readouterr().err
    registry = repo / freeze.MODEL_REGISTRY
    text = registry.read_text()
    registry.write_text(
        text + "".join(f"{k}:\n{v}" for k, v in FIFTH_ARM_ENTRIES.items() if f"\n{k}:" not in text)
    )
    _git(repo, "commit", "-qam", "fifth arm", "--allow-empty")
    assert cli.main([*write, "--write"]) == 0
    capsys.readouterr()
    written = freeze.recorded_v3((repo / freeze.SPEC_V3).read_text())
    assert (written[freeze.M_ROW], written[freeze.DROPPED_ROW]) == ("20", "none")
    before = (repo / freeze.SPEC_V3).read_text()
    assert cli.main([*args, "--drop", "claude-sonnet-5-5"]) == 0
    assert "Dropped arms: claude-sonnet-5-5; Wave-wide m stays 20" in capsys.readouterr().err
    after = (repo / freeze.SPEC_V3).read_text()
    assert [line for line in after.split("\n") if line not in before.split("\n")] == [
        "| Dropped arms | `claude-sonnet-5-5` |"
    ]
    assert cli.main([*args, "--image-digest", DIGEST, "--check"]) == 0
    assert cli.main([*args, "--drop", "claude-sonnet-4-5"]) == 1
    assert "dropped arms must be confirmatory arms" in capsys.readouterr().err
    assert cli.main([*args, "--drop", "gpt-5.6-sol", "--arms", "x"]) == 2
    assert (repo / freeze.SPEC_V3).read_text() == after
    assert cli.main(["freeze-record", "--root", str(ROOT), "--drop", "gpt-5.6-sol"]) == 2
