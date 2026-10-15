from pathlib import Path

import pytest

from lost_in_place import freeze, labels
from lost_in_place.llm import prompt

ROOT = Path(__file__).resolve().parents[1]
SPEC_TEXT = (ROOT / "SPEC.md").read_text()
VALUES = {row: f"{i:064x}" for i, row in enumerate(freeze.ALL_ROWS)}
# A SPEC as it stood at freeze-v2, before the analysis script was pinned: the record has the
# freeze-v2 rows only. Synthetic, so the pre-pin cases don't depend on the repo's SPEC.md.
V2_VALUES = {row: VALUES[row] for row in freeze.ROWS}
UNPINNED_SPEC = "\n".join(
    [
        "# A spec",
        "",
        "Body text, with a Fault model.",
        "",
        freeze.RECORD_HEADING,
        "",
        "The freeze, in order: hash, tag, publish.",
        "",
        "| Artifact | Hash |",
        "|---|---|",
        *(f"| {row} | |" for row in freeze.ROWS),
        "",
        "## Amendment log",
        "",
        "| Date | Change | Why |",
        "|---|---|---|",
        "",
    ]
)


def test_spec_has_every_row_of_the_record() -> None:
    assert set(freeze.recorded(SPEC_TEXT)) == set(freeze.ALL_ROWS)


def test_spec_pins_the_current_analysis_script() -> None:
    # A change to `stats.py` is a dated amendment that re-pins it (SPEC.md, amendment log 2026-10-15).
    assert freeze.recorded(SPEC_TEXT)[freeze.ANALYSIS_ROW] == freeze.analysis_hash(ROOT)


def test_writing_the_record_does_not_change_the_spec_hash() -> None:
    written = freeze.write_record(SPEC_TEXT, VALUES)
    assert written != SPEC_TEXT
    assert freeze.spec_hash(written) == freeze.spec_hash(SPEC_TEXT)
    assert freeze.recorded(written) == VALUES


def test_any_other_spec_edit_changes_the_hash() -> None:
    assert freeze.spec_hash(SPEC_TEXT.replace("Fault", "Fault ", 1)) != freeze.spec_hash(SPEC_TEXT)


def test_mismatches_name_changed_rows_but_not_the_git_sha() -> None:
    written = freeze.write_record(SPEC_TEXT, VALUES)
    later = {**VALUES, freeze.ROWS[3]: "f" * 64, "Git SHA": "a" * 40}
    assert freeze.mismatches(written, VALUES) == []
    assert [m.split(":")[0] for m in freeze.mismatches(written, later)] == [freeze.ROWS[3]]


def test_scenario_sets_are_sorted_by_name_and_concatenated(tmp_path: Path) -> None:
    (tmp_path / "b.yaml").write_text("b\n")
    (tmp_path / "a.yaml").write_text("a\n")
    (tmp_path / "notes.txt").write_text("ignored")
    assert freeze.scenario_set_hash(tmp_path) == freeze.sha256(b"a\nb\n")
    with pytest.raises(FileNotFoundError):
        freeze.scenario_set_hash(tmp_path / "missing")


def test_the_prompt_hash_follows_the_wording(monkeypatch: pytest.MonkeyPatch) -> None:
    before = freeze.prompt_hash()
    assert freeze.prompt_hash() == before
    bundle = freeze.prompt_bundle()
    assert (
        "Estimator health:" in bundle["llm"]["user_o1"]
        and "Estimator health:" not in bundle["llm"]["user_o0"]
    )
    assert "Estimator health:" in bundle["dexi"]["prompt_o1"]
    monkeypatch.setattr(prompt, "SYSTEM_PROMPT", prompt.SYSTEM_PROMPT + " Be careful.")
    assert freeze.prompt_hash() != before


@pytest.mark.parametrize("name", ["INSTRUCTIONS", "BRIEF", "LABELLER_MODEL"])
def test_the_labelling_hash_follows_instructions_brief_and_model(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    before = freeze.labelling_hash()
    monkeypatch.setattr(labels, name, getattr(labels, name) + "x")
    assert freeze.labelling_hash() != before


def test_record_hashes_the_repo(tmp_path: Path) -> None:
    design = tmp_path / "design.md"
    design.write_text("design")
    values = freeze.record(ROOT, design, "sha256:" + "0" * 64)
    assert values["Experiment design"] == freeze.sha256(b"design")
    assert values["Image digest"] == "sha256:" + "0" * 64
    assert len(values["Git SHA"].removesuffix("-dirty")) == 40
    assert values[freeze.ANALYSIS_ROW] == freeze.sha256((ROOT / freeze.ANALYSIS_SCRIPT).read_bytes())
    assert freeze.record(ROOT, None, None)["Experiment design"] == ""


def test_an_unpinned_record_has_no_analysis_row_and_the_check_ignores_it_until_pinned() -> None:
    assert set(freeze.recorded(UNPINNED_SPEC)) == set(freeze.ROWS)
    written = freeze.write_record(UNPINNED_SPEC, V2_VALUES)
    assert freeze.ANALYSIS_ROW not in freeze.recorded(written)
    now = {**V2_VALUES, freeze.ANALYSIS_ROW: "a" * 64}
    assert freeze.mismatches(written, now) == []
    assert freeze.mismatches(written, now, require_analysis=True) == [
        f"{freeze.ANALYSIS_ROW}: recorded (blank), now {'a' * 64}"
    ]


def test_pinning_the_analysis_adds_its_row_and_refreshes_only_this_files_hash() -> None:
    before = freeze.write_record(UNPINNED_SPEC, V2_VALUES)
    pinned = freeze.write_analysis(before, ROOT)
    recorded = freeze.recorded(pinned)
    assert recorded[freeze.ANALYSIS_ROW] == freeze.analysis_hash(ROOT)
    assert recorded["This file"] == freeze.spec_hash(pinned) != VALUES["This file"]
    for row in freeze.ROWS[1:]:
        assert recorded[row] == VALUES[row]
    # The row sits in the record's table, after the git SHA, and the file is otherwise untouched.
    lines = pinned.split("\n")
    i = next(i for i, line in enumerate(lines) if line.startswith(f"| {freeze.ANALYSIS_ROW} |"))
    assert lines[i - 1].startswith("| Git SHA |") and lines[i + 1] == ""
    assert freeze.blank_record(pinned).replace(f"| {freeze.ANALYSIS_ROW} | |\n", "") == freeze.blank_record(
        before
    )
    # Now the plain check compares the analysis row too, and the pin is idempotent.
    now = {**VALUES, "This file": recorded["This file"], freeze.ANALYSIS_ROW: freeze.analysis_hash(ROOT)}
    assert freeze.mismatches(pinned, now) == []
    assert freeze.mismatches(pinned, now, require_analysis=True) == []
    changed = {**now, freeze.ANALYSIS_ROW: "b" * 64}
    assert [m.split(":")[0] for m in freeze.mismatches(pinned, changed)] == [freeze.ANALYSIS_ROW]
    assert freeze.write_analysis(pinned, ROOT) == pinned


def test_the_analysis_value_does_not_enter_the_spec_hash() -> None:
    pinned = freeze.write_analysis(SPEC_TEXT, ROOT)
    other = freeze.write_record(pinned, {freeze.ANALYSIS_ROW: "c" * 64}, (freeze.ANALYSIS_ROW,))
    assert other != pinned and freeze.spec_hash(other) == freeze.spec_hash(pinned)


def test_freeze_record_cli_pins_and_checks_the_analysis_script(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from lost_in_place import cli

    root = tmp_path / "repo"
    (root / "src" / "lost_in_place").mkdir(parents=True)
    (root / freeze.ANALYSIS_SCRIPT).write_bytes((ROOT / freeze.ANALYSIS_SCRIPT).read_bytes())
    (root / freeze.SPEC).write_text(UNPINNED_SPEC)
    assert cli.main(["freeze-record", "--root", str(root), "--analysis", "--check"]) == 1
    assert "recorded (blank)" in capsys.readouterr().err
    assert cli.main(["freeze-record", "--root", str(root), "--analysis"]) == 0
    out = capsys.readouterr().out
    assert f"path: {freeze.ANALYSIS_SCRIPT}" in out and f"SHA-256: {freeze.analysis_hash(root)}" in out
    assert (root / freeze.SPEC).read_text() == UNPINNED_SPEC  # printing changes nothing
    assert cli.main(["freeze-record", "--root", str(root), "--analysis", "--write"]) == 0
    capsys.readouterr()
    assert cli.main(["freeze-record", "--root", str(root), "--analysis", "--check"]) == 0
    (root / freeze.ANALYSIS_SCRIPT).write_bytes(b"# changed\n")
    assert cli.main(["freeze-record", "--root", str(root), "--analysis", "--check"]) == 1
    assert freeze.ANALYSIS_ROW in capsys.readouterr().err
