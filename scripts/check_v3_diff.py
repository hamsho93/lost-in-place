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
  (`freeze.probe_hash_v3()`) changed, or can't be computed. Each is computed from its own ref's code;
- any file under `scenarios/` changed;
- any other file changed that is on neither list: A-6's harness code (`HARNESS`) or A-7's
  mechanical fill-ins (`FILL_INS`). This file is on neither, so it can't be loosened in between.

A path ending in `/` covers everything under it. A file on the harness list can still fail through
the bundle hash: `llm/dexi.py` holds both DEXI's driver and the prompt texts. Exit 0 passes, 1 fails,
2 is a usage error. The report is Markdown, for the design's adaptation-outcomes block.
"""

from __future__ import annotations

import argparse
import dataclasses
import difflib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

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

# Run in a fresh interpreter on one ref's tree, so each hash comes from that ref's own code.
_HASHES = """
import json, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / "src"))
from lost_in_place import freeze
if not Path(freeze.__file__).resolve().is_relative_to(root):
    raise SystemExit(f"imported {freeze.__file__}, not the ref's code")
out = {}
calls = {"prompt bundle v3": freeze.prompt_hash_v3, "probe prompts v3": lambda: freeze.probe_hash_v3(root)}
for name, call in calls.items():
    try:
        out[name] = call()
    except Exception as e:
        out[name] = f"error: {type(e).__name__}: {e}"
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


def bundle_hashes(repo: Path, sha: str) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="check-v3-") as tmp:
        archive = subprocess.run(["git", "archive", sha], cwd=repo, check=True, capture_output=True)
        subprocess.run(["tar", "-x", "-C", tmp], input=archive.stdout, check=True)
        done = subprocess.run(
            [sys.executable, "-B", "-c", _HASHES, tmp], cwd=tmp, capture_output=True, text=True
        )
    if done.returncode:
        tail = (done.stderr.strip().splitlines() or ["no output"])[-1]
        return {"prompt bundle v3": f"error: {tail}", "probe prompts v3": f"error: {tail}"}
    hashes: dict[str, str] = json.loads(done.stdout)
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


def check_bundles(repo: Path, pre: str, head: str, result: Result) -> None:
    before, after = bundle_hashes(repo, pre), bundle_hashes(repo, head)
    for name in ("prompt bundle v3", "probe prompts v3"):
        b, a = before.get(name, "error: not computed"), after.get(name, "error: not computed")
        if b.startswith("error") or a.startswith("error"):
            result.failures.append(f"{name}: can't compute it (pre: {b}; head: {a})")
        elif b != a:
            result.failures.append(f"{name} changed: `{b}` at pre, `{a}` at head")
        else:
            result.notes.append(f"{name}: `{a}` at both refs")


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
