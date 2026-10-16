"""Freeze-v3's diff check (design section 2.4): what may change between `freeze-v3-pre` and the
adaptation pull request (WP18).

    uv run python scripts/check_v3_diff.py freeze-v3-pre HEAD [--repo .]

Run the pre tag's copy of this file, so the rules applied are the pre-registered ones:

    git show freeze-v3-pre:scripts/check_v3_diff.py > /tmp/check_v3_diff.py
    uv run python /tmp/check_v3_diff.py freeze-v3-pre HEAD

It fails if, between the two refs:
- the design document changed outside its marked blocks, or a block was added, removed or moved. A
  block opens with a line that is exactly `<!-- filled at freeze -->` and closes with a line that is
  exactly `<!-- end filled at freeze -->`; the marker text inside a longer line is prose;
- `SPEC-v3.md` changed anywhere but the values of its freeze record's table;
- the v3 prompt bundle's hash (`freeze.prompt_hash_v3()`) or the probe prompts' hash
  (`freeze.probe_hash_v3()`) changed, or can't be computed. Each is computed from its own ref's code
  and scenarios. The bundle holds the render grid (`prompt_grid`): every prompt DEXI's planner
  renders for each design level and prompt version across a sweep of health values, several histories
  and the T0 and T1 instructions of a sample of the E5 families and every canary, so a rendering
  change anywhere on that path fails, not only at the bundle's exemplars;
- any file under `scenarios/` changed, or a frozen render or probe fixture under `tests/fixtures/`
  (`FROZEN_FIXTURES`, or a name with render, golden, probe, prompt or wording in it);
- `cli.py` changed on a prompt or probe rendering path (`RENDERING`): a function there naming such
  an identifier or option, or calling one that does, may not change at all; the parser's statements
  may change only where they name none. This is a syntactic check: a change that reaches a rendering
  path only through data (a file written for the planner to read) is not caught here, but a change
  to what is rendered still fails the bundle hash;
- any other file changed that is on neither list: A-6's harness code (`HARNESS`) or A-7's
  mechanical fill-ins (`FILL_INS`). This file is on neither, so it can't be loosened in between.

A path ending in `/` covers everything under it. A file on the harness list can still fail through
the bundle hash: `llm/dexi.py` holds both DEXI's driver and the prompt texts. Exit 0 passes, 1 fails,
2 is a usage error. The report is Markdown, for the design's adaptation-outcomes block.
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import copy
import dataclasses
import difflib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

DESIGN = "docs/prereg/freeze-v3-design.md"
SPEC_V3 = "SPEC-v3.md"
SCENARIOS = "scenarios/"
OPEN = "<!-- filled at freeze -->"
CLOSE = "<!-- end filled at freeze -->"
RECORD_HEADING = "## Freeze record"
_ROW = re.compile(r"^\| (?P<artifact>.+?) \| (?P<value>[^|]*?) ?\|$")

# A-6: only harness code may change, to fix a failed canary harness check.
HARNESS = {
    "agent": (
        "src/lost_in_place/agent.py",
        "src/lost_in_place/episode.py",
        "src/lost_in_place/sim.py",
        "src/lost_in_place/slots.py",
        "src/lost_in_place/config.py",
        "src/lost_in_place/provenance.py",
        "src/lost_in_place/storage.py",
        "src/lost_in_place/worlds.py",
        "src/lost_in_place/planning.py",
        "src/lost_in_place/validators.py",
        "src/lost_in_place/llm/dexi.py",
        "src/lost_in_place/llm/clients.py",
        "src/lost_in_place/llm/planner.py",
        "src/lost_in_place/llm/registry.py",
    ),
    "scorer": (
        "src/lost_in_place/scoring.py",
        "src/lost_in_place/metrics.py",
        "src/lost_in_place/geometry.py",
    ),
    "scripted control": (
        "src/lost_in_place/scripted.py",
        "src/lost_in_place/llm/scripted_dexi.py",
    ),
    "submitter": (
        "scripts/e5_submit.py",
        "scripts/e5_meter.py",
        "src/lost_in_place/wave/",
        "docs/runbooks/e5-wave.md",
    ),
    "infra": ("infra/", "docker/", "px4/", ".github/workflows/"),
    "tests of a harness fix": ("tests/",),
}

# Frozen text under tests/, which a harness fix's tests may not change: the golden renders of what an
# arm is shown, and any fixture of a render or a probe.
FROZEN_FIXTURES = (
    "tests/fixtures/dexi_ladder/",
    "tests/fixtures/dexi_9b2e7a7/",
    "tests/fixtures/qwen3_instruct_render.txt",
    "tests/fixtures/qwen3_thinking_render.txt",
    "tests/fixtures/qwen3_thinking_request.json",
)
_FROZEN_NAME = re.compile(r"render|golden|probe|prompt|wording", re.IGNORECASE)

# A-7 allows `cli.py` for the stats_v3 pin (`lip stats --design v3`), but not on a prompt or probe
# rendering path. A function naming one of these, or calling or passing one that does, is on it and
# may not change. The parser, which the pin has to extend, is checked statement by statement: a
# changed statement may not name one either.
CLI = "src/lost_in_place/cli.py"
PARSER = "build_parser"
RENDERING = re.compile(
    r"prompt|observation|validator|driver|planner|propose|render|template|health|probe|wording"
    r"|instruction|mission|dexi|model|agent_?config|fly",
    re.IGNORECASE,
)
_WORD = re.compile(r"[-\w.:/]+")

# A-7: the mechanical fill-ins. The first two are checked further below.
FILL_INS = {
    "freeze record values": (SPEC_V3,),
    "marked blocks": (DESIGN,),
    "sweeps for the chosen N, arms and template": ("sweeps/e5/",),
    "the stats_v3 pin (the analysis code and `lip stats --design v3`)": (
        "src/lost_in_place/stats_v3.py",
        "src/lost_in_place/cli.py",
    ),
}

# Run in a fresh interpreter on one ref's tree, so each hash comes from that ref's own code. Besides
# the two hashes it prints a digest per part of the bundle, so a mismatch says where.
_HASHES = """
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / "src"))
from lost_in_place import freeze
if not Path(freeze.__file__).resolve().is_relative_to(root):
    raise SystemExit(f"imported {freeze.__file__}, not the ref's code")

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

out = {}
try:
    bundle = freeze.prompt_bundle_v3(root)
    out["prompt bundle v3"] = freeze.sha256(freeze._canonical(bundle))
    grid = bundle.get("render_grid", {})
    parts = {key: digest(value) for key, value in bundle.items() if key != "render_grid"}
    parts |= {f"render grid {pair}": h for pair, h in grid.get("by_pair", {}).items()}
    parts |= {f"wire render {name}": h for name, h in grid.get("wire", {}).items()}
    parts["render grid scenarios"] = digest(grid.get("scenarios"))
    out["parts"], out["prompts"] = parts, grid.get("prompts", 0)
except Exception as e:
    out["prompt bundle v3"] = f"error: {type(e).__name__}: {e}"
try:
    out["probe prompts v3"] = freeze.probe_hash_v3(root)
except Exception as e:
    out["probe prompts v3"] = f"error: {type(e).__name__}: {e}"
print(json.dumps(out))
"""


@dataclasses.dataclass
class Result:
    rows: list[tuple[str, str, str]] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    failures: list[str] = dataclasses.field(default_factory=list)


def _covers(pattern: str, path: str) -> bool:
    return path.startswith(pattern) if pattern.endswith("/") else path == pattern


def rule(path: str) -> tuple[str, bool]:
    """Which list allows a changed path, and whether it is allowed at all."""
    if path.startswith(SCENARIOS):
        return "scenario directory", False
    if any(_covers(p, path) for p in FROZEN_FIXTURES) or (
        path.startswith("tests/fixtures/") and _FROZEN_NAME.search(path)
    ):
        return "frozen render or probe fixture", False
    for lists, label in ((HARNESS, "A-6 harness"), (FILL_INS, "A-7 fill-in")):
        for category, patterns in lists.items():
            if any(_covers(p, path) for p in patterns):
                return f"{label}: {category}", True
    return "on neither the A-6 nor the A-7 list", False


def skeleton(text: str) -> tuple[list[str], int]:
    """The document outside its marked blocks, each block reduced to its two markers, and the number
    of blocks. Raises on a nested, unopened or unclosed block."""
    out, inside, blocks = [], False, 0
    for n, line in enumerate(text.split("\n"), 1):
        if line == OPEN:
            if inside:
                raise ValueError(f"line {n}: a block opens inside a block")
            inside, blocks = True, blocks + 1
            out.append(line)
        elif line == CLOSE:
            if not inside:
                raise ValueError(f"line {n}: a block closes without one open")
            inside = False
            out.append(line)
        elif not inside:
            out.append(line)
    if inside:
        raise ValueError("a block is never closed")
    return out, blocks


def blank_record(text: str) -> str:
    """The text with every value of the freeze record's table blank."""
    lines = text.split("\n")
    if RECORD_HEADING not in lines:
        return text
    start = lines.index(RECORD_HEADING)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            break
        m = _ROW.match(lines[i])
        if m:
            lines[i] = f"| {m['artifact']} | |"
    return "\n".join(lines)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def resolve(repo: Path, ref: str) -> str:
    done = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if done.returncode:
        raise ValueError(f"not a commit: {ref}")
    return done.stdout.strip()


def show(repo: Path, sha: str, path: str) -> str | None:
    done = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=repo, capture_output=True, text=True)
    return done.stdout if done.returncode == 0 else None


def changed(repo: Path, pre: str, head: str) -> list[tuple[str, str]]:
    fields = _git(repo, "diff", "--no-renames", "--name-status", "-z", pre, head).split("\0")
    return [(fields[i], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]


_BY_TREE: dict[str, dict[str, Any]] = {}


def bundle_hashes(repo: Path, sha: str) -> dict[str, Any]:
    """The bundle and probe hashes computed from `sha`'s own tree. A tree's result is kept for the
    process: the same tree is the same code and the same scenarios."""
    tree = _git(repo, "rev-parse", f"{sha}^{{tree}}").strip()
    if tree in _BY_TREE:
        return _BY_TREE[tree]
    with tempfile.TemporaryDirectory(prefix="check-v3-") as tmp:
        archive = subprocess.run(["git", "archive", sha], cwd=repo, check=True, capture_output=True)
        subprocess.run(["tar", "-x", "-C", tmp], input=archive.stdout, check=True)
        done = subprocess.run(
            [sys.executable, "-B", "-c", _HASHES, tmp], cwd=tmp, capture_output=True, text=True
        )
    if done.returncode:
        tail = (done.stderr.strip().splitlines() or ["no output"])[-1]
        return {"prompt bundle v3": f"error: {tail}", "probe prompts v3": f"error: {tail}"}
    hashes: dict[str, Any] = json.loads(done.stdout)
    _BY_TREE[tree] = hashes
    return hashes


def _diff(before: list[str], after: list[str], limit: int = 12) -> str:
    lines = [line for line in difflib.unified_diff(before, after, "pre", "head", n=0, lineterm="")][2:]
    shown = lines[:limit] + ([f"... {len(lines) - limit} more"] if len(lines) > limit else [])
    return "\n".join(f"    {line}" for line in shown)


def check_design(repo: Path, pre: str, head: str, result: Result) -> None:
    before, after = show(repo, pre, DESIGN), show(repo, head, DESIGN)
    if before is None or after is None:
        result.failures.append(f"`{DESIGN}` is missing at {'pre' if before is None else 'head'}")
        return
    try:
        (outside_before, blocks), (outside_after, _) = skeleton(before), skeleton(after)
    except ValueError as e:
        result.failures.append(f"`{DESIGN}`: malformed marked blocks: {e}")
        return
    if outside_before != outside_after:
        result.failures.append(
            f"`{DESIGN}` changed outside its marked blocks:\n{_diff(outside_before, outside_after)}"
        )
    else:
        result.notes.append(f"`{DESIGN}`: unchanged outside its {blocks} marked blocks")


def check_spec(repo: Path, pre: str, head: str, result: Result) -> None:
    before, after = show(repo, pre, SPEC_V3), show(repo, head, SPEC_V3)
    if before is None or after is None:
        result.failures.append(f"`{SPEC_V3}` is missing at {'pre' if before is None else 'head'}")
        return
    blank_before, blank_after = blank_record(before), blank_record(after)
    if blank_before != blank_after:
        lines_before, lines_after = blank_before.split("\n"), blank_after.split("\n")
        result.failures.append(
            f"`{SPEC_V3}` changed outside its freeze record's values:\n{_diff(lines_before, lines_after)}"
        )
    else:
        result.notes.append(f"`{SPEC_V3}`: unchanged outside its freeze record's values")


_FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)


def _tokens(node: ast.AST) -> set[str]:
    """The names a node uses or binds, and its one-word string constants (option names, choices)."""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, (*_FUNCTION, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.arg) or (isinstance(n, ast.keyword) and n.arg):
            out.add(n.arg)
        elif isinstance(n, ast.alias):
            out.update(name for name in (n.name, n.asname) if name)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and _WORD.fullmatch(n.value):
            out.add(n.value)
    return out


def rendering_functions(tree: ast.Module) -> set[str]:
    """The module's functions on a prompt or probe rendering path: those naming one, and those that
    call or pass one."""
    used = {n.name: _tokens(n) for n in tree.body if isinstance(n, _FUNCTION)}
    path = {name for name, tokens in used.items() if any(RENDERING.search(t) for t in tokens)}
    while grown := {name for name, tokens in used.items() if name not in path and tokens & path}:
        path |= grown
    return path


def _units(tree: ast.Module) -> dict[str, list[ast.AST]]:
    """The module's statements by where they are: each function's signature and body statements,
    and the statements outside any function."""
    units: dict[str, list[ast.AST]] = {"module level": []}
    for node in tree.body:
        if isinstance(node, _FUNCTION):
            signature = copy.copy(node)
            signature.body = []
            units[node.name] = [signature, *node.body]
        else:
            units["module level"].append(node)
    return units


def cli_changes(before: str, after: str) -> tuple[list[str], int]:
    """The changed statements of `cli.py` that are on a prompt or probe rendering path, described,
    and the number of changed statements."""
    trees = ast.parse(before), ast.parse(after)
    path = rendering_functions(trees[0]) | rendering_functions(trees[1])
    units = _units(trees[0]), _units(trees[1])
    problems, count = [], 0
    for where in dict.fromkeys([*units[0], *units[1]]):
        old, new = units[0].get(where, []), units[1].get(where, [])
        whole = where in path and where != PARSER
        label = where if where == "module level" else f"`{where}`"
        matcher = difflib.SequenceMatcher(
            a=[ast.dump(s) for s in old], b=[ast.dump(s) for s in new], autojunk=False
        )
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            for side, statements in (("pre", old[i1:i2]), ("head", new[j1:j2])):
                for statement in statements:
                    count += 1
                    line = getattr(statement, "lineno", "?")
                    named = sorted(t for t in _tokens(statement) if RENDERING.search(t) or t in path)
                    if whole:
                        problems.append(
                            f"{label}, line {line} at {side}: the function is on a rendering path"
                        )
                    elif named:
                        problems.append(f"{label}, line {line} at {side}: names {', '.join(named[:4])}")
    return problems, count


def check_cli(repo: Path, pre: str, head: str, result: Result) -> None:
    before, after = show(repo, pre, CLI), show(repo, head, CLI)
    if before == after:
        return
    try:
        problems, count = cli_changes(before or "", after or "")
    except SyntaxError as e:
        result.failures.append(f"`{CLI}`: can't parse it: {e}")
        return
    if problems:
        shown = "\n".join(f"    {p}" for p in problems[:12])
        result.failures.append(f"`{CLI}` changes a prompt or probe rendering path:\n{shown}")
    else:
        result.notes.append(f"`{CLI}`: {count} changed statements, none on a prompt or probe rendering path")


def check_bundles(repo: Path, pre: str, head: str, result: Result) -> None:
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        before, after = pool.map(lambda sha: bundle_hashes(repo, sha), (pre, head))
    for name in ("prompt bundle v3", "probe prompts v3"):
        b, a = before.get(name, "error: not computed"), after.get(name, "error: not computed")
        if b.startswith("error") or a.startswith("error"):
            result.failures.append(f"{name}: can't compute it (pre: {b}; head: {a})")
        elif b != a:
            parts_b, parts_a = before.get("parts", {}), after.get("parts", {})
            where = sorted(p for p in {*parts_b, *parts_a} if parts_b.get(p) != parts_a.get(p))
            detail = f"; differs in: {', '.join(where)}" if where else ""
            result.failures.append(f"{name} changed: `{b}` at pre, `{a}` at head{detail}")
        else:
            extra = f" ({after['prompts']} prompts rendered from each ref)" if after.get("prompts") else ""
            result.notes.append(f"{name}: `{a}` at both refs{extra}")


def check(repo: Path, pre_ref: str, head_ref: str) -> Result:
    pre, head = resolve(repo, pre_ref), resolve(repo, head_ref)
    result = Result()
    for status, path in changed(repo, pre, head):
        where, allowed = rule(path)
        result.rows.append((path, status, where if allowed else f"**not allowed**: {where}"))
        if not allowed:
            result.failures.append(f"`{path}` ({status}): {where}")
    check_design(repo, pre, head, result)
    check_spec(repo, pre, head, result)
    check_cli(repo, pre, head, result)
    check_bundles(repo, pre, head, result)
    return result


def report(result: Result, pre_ref: str, head_ref: str, pre: str, head: str) -> str:
    out = [f"### check_v3_diff: `{pre_ref}` ({pre[:12]}) to `{head_ref}` ({head[:12]})", ""]
    if result.rows:
        out += ["| Path | Change | Rule |", "|---|---|---|"]
        out += [f"| `{path}` | {status} | {where} |" for path, status, where in result.rows]
    else:
        out.append("No file changed.")
    out += ["", *(f"- {note}" for note in result.notes), ""]
    if result.failures:
        out += [f"**FAIL**: {len(result.failures)} problem(s)", "", *(f"- {f}" for f in result.failures)]
    else:
        out.append("**PASS**")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("pre", help="the pre-registration ref, normally freeze-v3-pre")
    parser.add_argument("head", help="the ref to check, normally HEAD")
    parser.add_argument("--repo", type=Path, default=Path(), help="the git checkout (default: .)")
    args = parser.parse_args(argv)
    try:
        pre, head = resolve(args.repo, args.pre), resolve(args.repo, args.head)
    except (ValueError, subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"check_v3_diff: {e}", file=sys.stderr)
        return 2
    result = check(args.repo, pre, head)
    print(report(result, args.pre, args.head, pre, head))
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
