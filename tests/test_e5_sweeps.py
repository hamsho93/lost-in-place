"""The E5 sweep generator (`scripts/e5_sweeps.py`): every generated sweep, expanded by `lip manifest`,
has the design's episode counts, family subsets and ladder levels (freeze-v3 design, sections 5, 6
and 10.1)."""

import collections
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from lost_in_place import cli
from lost_in_place.llm.dexi import LADDER, ladder_level
from lost_in_place.planning import arm_name

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "e5_sweeps.py"
SIX = {"O0", "O1", "O1-L", "O1-N", "O1-R", "O1-V"}


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("e5_sweeps", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _names(prefix: str, n: int) -> set[str]:
    return {f"{prefix}{i:03d}" for i in range(n)}


def expand(path: Path, out: Path, capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    """`lip manifest <path>`, as the operator runs it, from the repo root."""
    assert cli.main(["manifest", str(path), "--root", str(ROOT), "--out", str(out)]) == 0
    printed = json.loads(capsys.readouterr().out.splitlines()[0])
    entries: list[dict[str, Any]] = json.loads(out.read_text())["entries"]
    assert printed["episodes"] == len(entries) == printed["scenario_cells"]
    return entries


def cells(entries: list[dict[str, Any]]) -> dict[tuple[str, str, str], set[str]]:
    """{(arm, state, ladder level): families}; the level from the observation and prompt option."""
    out: dict[tuple[str, str, str], set[str]] = collections.defaultdict(set)
    for e in entries:
        family, state = e["scenario_id"].split("_", 1)
        level = ladder_level(e["observation"], e["planner_options"].get("prompt", ""))
        out[arm_name(e["planner"], e["planner_options"]), state, level or e["observation"]].add(family)
    return out


@pytest.mark.parametrize("n", [6, 30, 60, 99, 100, 120, 160])
def test_family_globs_match_exactly_the_first_n_names(gen: ModuleType, n: int) -> None:
    import fnmatch

    every = {f"c{i:03d}" for i in range(1000)}
    matched = {name for name in every if any(fnmatch.fnmatchcase(name, g) for g in gen.family_globs("c", n))}
    assert matched == _names("c", n)


@pytest.mark.parametrize(
    ("template", "n", "gpt"), [("t0", 120, "gpt-6.1-sol"), ("t1", 160, "gpt-6.1-sol-fb2")]
)
def test_wave_sweeps_have_the_designs_cells(
    gen: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], template: str, n: int, gpt: str
) -> None:
    files = gen.wave(template, n, gen.ARMS, gpt)
    assert set(files) == {f"{a}.yaml" for a in gen.ARMS}
    assert gen.write(files, tmp_path, check=False) == 0
    capsys.readouterr()
    assert gen.write(files, tmp_path, check=True) == 0
    expected = {120: (2040, 1440), 160: (2560, 1920)}[n]
    names: set[str] = set()
    for arm in gen.ARMS:
        entries = expand(tmp_path / f"{arm}.yaml", tmp_path / f"{arm}.json", capsys)
        doc = json.loads((tmp_path / f"{arm}.json").read_text())
        names.add(doc["name"])
        assert doc["spec"]["shuffle_seed"] == 0
        assert all(e["validator"] == "none" and e["latency_s"] == 2.0 and e["rep"] == 0 for e in entries)
        assert all(e["planner"] == "dexi" and e["planner_options"]["driver"] == "dexi" for e in entries)
        assert all(e["scenario"].startswith(f"scenarios/e5/{template}/") for e in entries)
        got = cells(entries)
        if arm == gen.SCRIPTED_ARM:
            assert len(entries) == expected[1]
            for model in ("rule-2m", "rule-leg"):
                key = f"dexi-{model}.driver-dexi.prompt-dexi-v2"
                want = {(key, s, lv) for s, lv in [("loud", "O1"), ("loud", "O0"), ("loud_timed", "O1")]}
                want |= {(key, s, "O1") for s in ("brief", "healthy", "flowscale")}
                assert {k for k in got if k[0] == key} == want
                assert all(got[k] == _names("c", n) for k in want)
            continue
        model = gpt if arm == gen.GPT_ARM else arm
        assert len(entries) == expected[0]
        assert {e["planner_options"]["model"] for e in entries} == {model}
        by_state: dict[str, set[str]] = collections.defaultdict(set)
        for (key, state, level), families in got.items():
            assert key == f"dexi-{model}.driver-dexi.prompt-{LADDER[level][1]}"
            by_state[state].add(level)
            want_n = {"loud": n, "brief": n, "healthy": 60}.get(state, 30)
            assert families == _names("c", want_n), (state, level)
        assert by_state == {
            "loud": set(LADDER),
            "brief": SIX,
            "healthy": SIX,
            "flowscale": {"O1", "O1-N"},
            "misaligned": {"O1", "O1-N"},
        }
    assert len(names) == len(gen.ARMS)


def test_the_arm_set_and_parameters_are_checked(gen: ModuleType) -> None:
    files = gen.wave("t1", 120, ("claude-sonnet-4-6", "scripted"), "gpt-6.1-sol")
    assert set(files) == {"claude-sonnet-4-6.yaml", "scripted.yaml"}
    with pytest.raises(ValueError, match="N 140"):
        gen.wave("t0", 140, gen.ARMS, "gpt-6.1-sol")
    with pytest.raises(ValueError, match="entry"):
        gen.wave("t0", 120, gen.ARMS, "gpt-6.1-sol-fb4")
    with pytest.raises(ValueError, match="arms"):
        gen.wave("t0", 120, ("qwen3-4b",), "gpt-6.1-sol")


def test_a_wrong_count_stops_the_generator(gen: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(gen.EXPECTED, 120, {"llm": 2039, "scripted": 1440})
    with pytest.raises(AssertionError, match="2040 episodes"):
        gen.wave("t0", 120, ("claude-sonnet-4-6",), "gpt-6.1-sol")


def test_check_reports_changed_and_stale_files(gen: ModuleType, tmp_path: Path) -> None:
    files = {"a.yaml": "x\n"}
    gen.write(files, tmp_path, check=False)
    (tmp_path / "b.yaml").write_text("y\n")
    assert gen.write(files, tmp_path, check=True) == 1
    (tmp_path / "b.yaml").unlink()
    (tmp_path / "a.yaml").write_text("z\n")
    assert gen.write(files, tmp_path, check=True) == 1


def test_the_committed_canary_sweeps_are_the_generators(
    gen: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    committed = ROOT / "sweeps" / "e5-canary"
    assert gen.write(gen.canary(), committed, check=True) == 0
    capsys.readouterr()
    llm = (*gen.LLM_MODELS, *gen.GPT_ENTRIES)
    assert {p.stem for p in committed.glob("*.yaml")} == {
        f"{t}-{a}" for t in gen.TEMPLATES for a in (*llm, gen.SCRIPTED_ARM)
    }
    kfam = _names("k", 6)
    for path in sorted(committed.glob("*.yaml")):
        template, arm = path.stem.split("-", 1)
        entries = expand(path, tmp_path / f"{path.stem}.json", capsys)
        assert json.loads((tmp_path / f"{path.stem}.json").read_text())["name"] == f"e5-canary-{path.stem}"
        assert all(e["scenario"].startswith(f"scenarios/e5/{template}/canary/") for e in entries)
        assert all(e["validator"] == "none" and e["rep"] == 0 for e in entries)
        got = cells(entries)
        if arm == gen.SCRIPTED_ARM:
            assert len(entries) == 18
            keys = {f"dexi-{m}.driver-dexi.prompt-dexi-v2" for m in gen.SCRIPTED_MODELS} | {
                "scripted-cautious"
            }
            assert got == {(k, "loud", "O1"): kfam for k in keys}
            continue
        assert len(entries) == 78
        want = {(f"dexi-{arm}.driver-dexi.prompt-{LADDER[lv][1]}", "loud", lv) for lv in LADDER}
        want |= {(f"dexi-{arm}.driver-dexi.prompt-{LADDER[lv][1]}", "brief", lv) for lv in SIX}
        assert got == dict.fromkeys(want, kfam)
