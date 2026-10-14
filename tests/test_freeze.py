from pathlib import Path

import pytest

from lost_in_place import freeze, labels
from lost_in_place.llm import prompt

ROOT = Path(__file__).resolve().parents[1]
SPEC_TEXT = (ROOT / "SPEC.md").read_text()
VALUES = {row: f"{i:064x}" for i, row in enumerate(freeze.ROWS)}


def test_spec_has_every_row_of_the_record() -> None:
    assert set(freeze.recorded(SPEC_TEXT)) == set(freeze.ROWS)


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
    assert freeze.record(ROOT, None, None)["Experiment design"] == ""
