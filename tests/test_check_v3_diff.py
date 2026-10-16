import importlib.util
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from lost_in_place import freeze

ROOT = Path(__file__).resolve().parents[1]
DESIGN = "\n".join(
    [
        "# Design",
        "",
        "It has blocks marked `<!-- filled at freeze -->`, filled between the tags.",
        "",
        "### 2.6 Pre-freeze reviews",
        "",
        "<!-- filled at freeze -->",
        "The four reviews.",
        "<!-- end filled at freeze -->",
        "",
        "### 2.7 Adaptation outcomes",
        "",
        "<!-- filled at freeze -->",
        "Filled by the adaptation pull request.",
        "<!-- end filled at freeze -->",
        "",
        "## 3. Hypotheses",
        "",
        "H1: the planner holds.",
        "",
    ]
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_v3_diff", ROOT / "scripts" / "check_v3_diff.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_v3_diff"] = module
    spec.loader.exec_module(module)
    return module


check_v3_diff = _load()


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


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *GIT_CONFIG, *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


SCENARIOS = [
    f"{freeze.E5_SCENARIOS}/{t}/{kind}/{name}_healthy.yaml"
    for t in freeze.E5_TEMPLATES
    for kind, names in (("families", ("c000", "c001")), ("canary", ("k000",)))
    for name in names
]


@pytest.fixture
def same_bundles(monkeypatch: pytest.MonkeyPatch) -> None:
    """For checks of the paths and documents: the bundle hashes, which take a few seconds, agree."""
    monkeypatch.setattr(
        check_v3_diff, "bundle_hashes", lambda repo, sha: {"prompt bundle v3": "b", "probe prompts v3": "p"}
    )


@pytest.fixture(scope="module")
def pre_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A repo tagged `pre` with the package's code, a design with two marked blocks, SPEC-v3.md and
    two families and a canary of each template, enough for the render grid."""
    repo = tmp_path_factory.mktemp("pre") / "repo"
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(ROOT / "src", repo / "src", ignore=ignore)
    shutil.copytree(ROOT / "scripts", repo / "scripts", ignore=ignore)
    wording = repo / "src" / "lost_in_place" / "wording.py"
    if not wording.exists():
        wording.write_text('PROBE_QUESTION = "Where is the point the mission says to hold at?"\n')
    shutil.copy(ROOT / freeze.SPEC_V3, repo / freeze.SPEC_V3)
    for scenario in SCENARIOS:
        (repo / scenario).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / scenario, repo / scenario)
    files = {freeze.DESIGN_V3: DESIGN, "README.md": "readme\n"}
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "pre")
    _git(repo, "tag", "pre")
    return repo


@pytest.fixture
def repo(pre_repo: Path, tmp_path: Path) -> Path:
    clone = tmp_path / "clone"
    subprocess.run(["git", *GIT_CONFIG, "clone", "-q", str(pre_repo), str(clone)], check=True)
    return clone


def _edit(repo: Path, path: str, change: Callable[[str], str]) -> None:
    target = repo / path
    old = target.read_text() if target.exists() else ""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(change(old))


def _run(repo: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "head")
    code = check_v3_diff.main(["pre", "HEAD", "--repo", str(repo)])
    return code, capsys.readouterr().out


def test_marked_block_edits_and_fill_ins_pass(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _edit(
        repo,
        freeze.DESIGN_V3,
        lambda t: t.replace("Filled by the adaptation pull request.", "A-1: T1.\nA-2: 120."),
    )
    values = {row: "a" * 64 for row in freeze.V3_ROWS} | freeze.Adaptation("t1", 120, ("arm",)).values()
    _edit(repo, freeze.SPEC_V3, lambda t: freeze.write_record_v3(t, values))
    _edit(repo, "sweeps/e5/e5-claude.yaml", lambda _: "sweep\n")
    _edit(repo, "src/lost_in_place/stats_v3.py", lambda _: "PINNED = True\n")
    _edit(repo, "src/lost_in_place/agent.py", lambda t: t + "\nHARNESS_FIX = 1\n")
    _edit(repo, "src/lost_in_place/llm/dexi.py", lambda t: t + "\nDRIVER_FIX = 1\n")
    _edit(repo, "infra/README.md", lambda t: t + "fix\n")
    _edit(repo, "tests/test_fix.py", lambda _: "def test_fix() -> None: ...\n")
    code, out = _run(repo, capsys)
    assert code == 0, out
    assert "**PASS**" in out and "unchanged outside its 2 marked blocks" in out
    assert "| `src/lost_in_place/llm/dexi.py` | M | A-6 harness: agent |" in out
    assert "| `sweeps/e5/e5-claude.yaml` | A | A-7 fill-in: sweeps" in out
    assert f"prompt bundle v3: `{freeze.prompt_hash_v3(repo)}` at both refs" in out
    assert "prompts rendered from each ref" in out


def test_the_scripts_bundle_hash_is_the_records(repo: Path) -> None:
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
    hashes = check_v3_diff.bundle_hashes(repo, sha.stdout.strip())
    assert hashes["prompt bundle v3"] == freeze.prompt_hash_v3(repo)


@pytest.mark.usefixtures("same_bundles")
def test_no_change_passes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _run(repo, capsys)
    assert code == 0 and "No file changed." in out


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda t: t.replace("H1: the planner holds.", "H1: the planner holds or lands."),
            "outside its marked blocks",
        ),
        (lambda t: t.replace("It has blocks", "It has three blocks"), "outside its marked blocks"),
        (lambda t: t + "Appended.\n", "outside its marked blocks"),
        (
            lambda t: t.replace("<!-- end filled at freeze -->\n\n### 2.7", "\n### 2.7", 1),
            "malformed marked blocks",
        ),
        (
            lambda t: t.replace(
                "H1: the planner holds.", "<!-- filled at freeze -->\nH1.\n<!-- end filled at freeze -->"
            ),
            "outside its marked blocks",
        ),
        (
            lambda t: t.replace(
                "<!-- filled at freeze -->\nThe four reviews.\n<!-- end filled at freeze -->\n", ""
            ),
            "outside its marked blocks",
        ),
    ],
    ids=["text", "prose-mentioning-a-marker", "append", "unclosed-block", "new-block", "removed-block"],
)
@pytest.mark.usefixtures("same_bundles")
def test_any_design_edit_outside_the_blocks_fails(
    repo: Path, capsys: pytest.CaptureFixture[str], change: Callable[[str], str], expected: str
) -> None:
    _edit(repo, freeze.DESIGN_V3, change)
    code, out = _run(repo, capsys)
    assert code == 1 and "**FAIL**" in out and expected in out


def test_a_prompt_bundle_change_fails_even_in_a_harness_file(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit(repo, "src/lost_in_place/llm/dexi.py", lambda t: t + '\nO1R_RULE = O1R_RULE + " Then land."\n')
    code, out = _run(repo, capsys)
    assert code == 1 and "prompt bundle v3 changed" in out
    assert "differs in: " in out and "o1r_rule" in out
    assert "| `src/lost_in_place/llm/dexi.py` | M | A-6 harness: agent |" in out


def test_a_render_change_off_the_bundles_exemplars_fails(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    patch = (
        "\n_health_lines = health_lines\n\n\n"
        "def health_lines(h):\n"
        "    lines = _health_lines(h)\n"
        "    return [line + ' (borderline)' for line in lines] if h.flow_quality == 49.5 else lines\n"
    )
    _edit(repo, "src/lost_in_place/llm/dexi.py", lambda t: t + patch)
    code, out = _run(repo, capsys)
    assert code == 1 and "prompt bundle v3 changed" in out
    differs = out.split("differs in: ")[1].split("\n")[0]
    assert "render grid O1/dexi-v2" in differs and "renders" not in differs.split(", ")


def test_a_probe_text_change_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _edit(repo, "src/lost_in_place/wording.py", lambda t: t + '\nPROBE_QUESTION = PROBE_QUESTION + " Now."\n')
    code, out = _run(repo, capsys)
    assert code == 1 and "probe prompts v3 changed" in out


@pytest.mark.parametrize(
    "path", ["scenarios/e5/t0/families/c000_healthy.yaml", "scenarios/e5/t1/canary/k000_loud.yaml"]
)
@pytest.mark.usefixtures("same_bundles")
def test_any_scenario_change_fails(repo: Path, capsys: pytest.CaptureFixture[str], path: str) -> None:
    _edit(repo, path, lambda t: t + "# edited\n")
    code, out = _run(repo, capsys)
    assert code == 1 and f"`{path}`" in out and "scenario directory" in out


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "src/lost_in_place/freeze.py",
        "src/lost_in_place/families.py",
        "src/lost_in_place/llm/prompt.py",
        "src/lost_in_place/llm/models.yaml",
        "scripts/check_v3_diff.py",
        "sweeps/e4-claude.yaml",
        "docs/prereg/freeze-v3-signoff.md",
    ],
)
@pytest.mark.usefixtures("same_bundles")
def test_a_file_on_neither_list_fails(repo: Path, capsys: pytest.CaptureFixture[str], path: str) -> None:
    _edit(repo, path, lambda t: t + "\n# edited\n")
    code, out = _run(repo, capsys)
    assert code == 1 and f"`{path}`" in out and "on neither the A-6 nor the A-7 list" in out


@pytest.mark.usefixtures("same_bundles")
def test_a_spec_v3_edit_outside_the_record_values_fails(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit(repo, freeze.SPEC_V3, lambda t: t.replace("Tango", "Newcombe", 1))
    code, out = _run(repo, capsys)
    assert code == 1 and "changed outside its freeze record's values" in out


@pytest.mark.usefixtures("same_bundles")
def test_a_renamed_record_row_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _edit(repo, freeze.SPEC_V3, lambda t: t.replace("| Wave-wide m | |", "| Wave-wide m (A-5) | |"))
    code, out = _run(repo, capsys)
    assert code == 1 and "changed outside its freeze record's values" in out


def test_a_ref_that_does_not_resolve_is_a_usage_error(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_v3_diff.main(["freeze-v3-pre", "HEAD", "--repo", str(repo)]) == 2
    assert "not a commit: freeze-v3-pre" in capsys.readouterr().err


def test_markers_count_only_as_whole_lines() -> None:
    text = (
        "a\nsee `<!-- filled at freeze -->`\n<!-- filled at freeze -->\nx\n<!-- end filled at freeze -->\nb"
    )
    assert check_v3_diff.skeleton(text) == (
        [
            "a",
            "see `<!-- filled at freeze -->`",
            "<!-- filled at freeze -->",
            "<!-- end filled at freeze -->",
            "b",
        ],
        1,
    )
    for bad in [
        "<!-- filled at freeze -->\n<!-- filled at freeze -->",
        "<!-- end filled at freeze -->",
        "<!-- filled at freeze -->",
    ]:
        with pytest.raises(ValueError):
            check_v3_diff.skeleton(bad)


def test_the_design_markers_are_freeze_v3s() -> None:
    assert (check_v3_diff.DESIGN, check_v3_diff.SPEC_V3) == (freeze.DESIGN_V3, freeze.SPEC_V3)
    assert check_v3_diff.RECORD_HEADING == freeze.RECORD_HEADING
    assert (check_v3_diff.OPEN, check_v3_diff.CLOSE) == (
        "<!-- filled at freeze -->",
        "<!-- end filled at freeze -->",
    )
    design = ROOT / freeze.DESIGN_V3
    if design.exists():
        assert check_v3_diff.skeleton(design.read_text())[1] == 3


@pytest.mark.parametrize(
    "path",
    [
        "tests/fixtures/dexi_ladder/O1-L_loud.txt",
        "tests/fixtures/dexi_ladder/O2_loud.txt",
        "tests/fixtures/dexi_9b2e7a7/tools.json",
        "tests/fixtures/qwen3_thinking_render.txt",
        "tests/fixtures/qwen3_thinking_request.json",
        "tests/fixtures/wording_probe.json",
        "tests/fixtures/sonnet_golden_prompt.txt",
    ],
)
def test_frozen_render_and_probe_fixtures_may_not_change(path: str) -> None:
    assert check_v3_diff.rule(path) == ("frozen render or probe fixture", False)


@pytest.mark.parametrize(
    "path",
    ["tests/test_agent.py", "tests/test_fix.py", "tests/fixtures/scoring/healthy_geometric/score.json"],
)
def test_other_tests_and_fixtures_are_a_harness_fixs(path: str) -> None:
    assert check_v3_diff.rule(path) == ("A-6 harness: tests of a harness fix", True)


@pytest.mark.usefixtures("same_bundles")
def test_a_golden_render_edit_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _edit(repo, "tests/fixtures/dexi_ladder/O1_loud.txt", lambda t: t + "edited\n")
    _edit(repo, "tests/test_fix.py", lambda _: "def test_fix() -> None: ...\n")
    code, out = _run(repo, capsys)
    assert code == 1 and "`tests/fixtures/dexi_ladder/O1_loud.txt` (A): frozen render or probe fixture" in out
    assert "`tests/test_fix.py` (A)" not in out.split("**FAIL**")[1]


CLI_TEXT = (ROOT / check_v3_diff.CLI).read_text()
STATS_OPTION = (
    '    st.add_argument("--out", type=Path, help="write <out>.json and <out>.md instead of printing")\n'
)
STATS_V3 = (
    "\n\ndef _cmd_stats_v3(args: argparse.Namespace) -> int:\n"
    "    from lost_in_place import stats_v3\n\n"
    "    print(stats_v3.markdown(stats_v3.analyse(args.runs)))\n"
    "    return 0\n"
)


def test_cli_edits_for_the_stats_pin_pass() -> None:
    assert STATS_OPTION in CLI_TEXT
    after = CLI_TEXT.replace(
        STATS_OPTION, STATS_OPTION + '    st.add_argument("--design", choices=["v2", "v3"], default="v2")\n'
    )
    after = after.replace("\n\ndef _analyze(", STATS_V3 + "\n\ndef _analyze(", 1)
    problems, count = check_v3_diff.cli_changes(CLI_TEXT, after)
    assert (problems, count) == ([], 2 + 3)


@pytest.mark.parametrize(
    ("old", "new", "where"),
    [
        (
            '"--planner", default="scripted-naive"',
            '"--planner", default="scripted-cautious"',
            "`build_parser`",
        ),
        (
            '        "--validator",\n        default="none",',
            '        "--validator",\n        default="oracle",',
            "`build_parser`",
        ),
        (
            "def _agent_config(args: argparse.Namespace) -> AgentConfig:\n",
            "def _agent_config(args: argparse.Namespace) -> AgentConfig:\n    args.observation = 'O1'\n",
            "`_agent_config`",
        ),
        (
            "def _cmd_propose(args: argparse.Namespace) -> int:\n",
            "def _cmd_propose(args: argparse.Namespace) -> int:\n    args = _tweak(args)\n",
            "`_cmd_propose`",
        ),
    ],
    ids=["planner-default", "validator-default", "agent-config", "propose"],
)
def test_cli_edits_on_a_rendering_path_fail(old: str, new: str, where: str) -> None:
    assert old in CLI_TEXT
    after = CLI_TEXT.replace(old, new, 1)
    problems, _ = check_v3_diff.cli_changes(CLI_TEXT, after)
    assert any(p.startswith(where) for p in problems), problems


def test_a_new_helper_is_on_the_path_when_it_names_a_rendering_option() -> None:
    import ast

    helper = "\n\ndef _tweak(args):\n    args.prompt_version = 'dexi-9b2e7a7'\n    return args\n"
    caller = "\n\ndef _cmd_other(args):\n    return _tweak(args)\n"
    tree = ast.parse(CLI_TEXT + helper + caller)
    assert {"_tweak", "_cmd_other", "_cmd_propose", "_agent_config"} <= check_v3_diff.rendering_functions(
        tree
    )
    assert "_cmd_stats" not in check_v3_diff.rendering_functions(tree)
    problems, _ = check_v3_diff.cli_changes(CLI_TEXT, CLI_TEXT + caller)
    assert problems == []
    problems, _ = check_v3_diff.cli_changes(CLI_TEXT, CLI_TEXT + helper + caller)
    assert any(p.startswith("`_tweak`") for p in problems) and any(
        p.startswith("`_cmd_other`") for p in problems
    )


@pytest.mark.usefixtures("same_bundles")
def test_the_check_reads_cli_py_at_both_refs(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _edit(
        repo, check_v3_diff.CLI, lambda t: t.replace("\n\ndef _analyze(", STATS_V3 + "\n\ndef _analyze(", 1)
    )
    code, out = _run(repo, capsys)
    assert code == 0 and "none on a prompt or probe rendering path" in out, out
    _edit(repo, check_v3_diff.CLI, lambda t: t.replace('default="scripted-naive"', 'default="llm"', 1))
    code, out = _run(repo, capsys)
    assert code == 1 and "changes a prompt or probe rendering path" in out and "`build_parser`" in out
