"""E5b (SPEC-v3 amendment of 2026-10-21): three Bedrock arms flown on the frozen E5 protocol, each
descriptive under freeze-v3 (design section 9.3, "Later models").

    uv run python scripts/e5b.py sweeps [--deepseek deepseek-v3-2-fb1] [--mistral mistral-large-3-fb1] \\
        [--check]
    uv run python scripts/e5b.py check
    uv run --extra aws python scripts/e5b.py canary-health s3://<bucket>/runs --usd llama-3-3-70b=2.10 ... \\
        --out runs/e5b/canary-health.json
    uv run python scripts/e5b.py admission --smoke runs/e5b/smoke.json --canary runs/e5b/canary-health.json
    uv run --extra aws python scripts/e5b.py analyse s3://<bucket>/runs --out docs/results/e5b-analysis

`sweeps` writes, with `scripts/e5_sweeps.py`'s builders (so E5's cells, families, levels, seeds and
shuffle):
- the wave sweeps, `sweeps/e5b/<arm>.yaml`: template T1, N = 120, each flying the registry entry its
  arm's smoke call accepted; and `sweeps/e5b/scripted.yaml`, E5's scripted control, of which the wave
  flies the S-1 gate cells again. Only the sweep names differ from E5's (`e5b-...`), so each has its own
  runs prefix and manifest.
- the canary sweeps, `sweeps/e5b-canary/t1-<entry>.yaml`, one per entry (fallbacks included). These keep
  E5's names, `e5-canary-t1-<entry>`; no E5 entry is among them, and the frozen
  `scripts/e5_admission.py` then reads them unchanged.

`check` is the E5b preflight. It passes only if E5's freeze record, as it stands, still describes
everything E5 flew:
- `lip freeze-record --freeze v3 --check` differs in the model registry and prompt bundle rows only;
- the registry recorded at `freeze-v3` is a byte prefix of this one, and what follows it is exactly the
  E5b entries;
- the prompt bundle, without the E5b entries' wire digests, hashes to the recorded value, so every E5
  prompt and every E5 arm's request is byte-identical;
and if the wave sweeps and config are written, each arm's sweep and config name the same entry of that
arm. Every value SPEC-v3's E5b record holds must equal this checkout's, and `docker/Dockerfile.e5b`
must build on the frozen image digest; with `--image-digest`, so must the recorded E5b image. It then
prints the E5b values.

`canary-health` runs the frozen `e5_admission.canary_health` over the E5b canaries, reading only its
score fields, and appends the read to the wave's queries log (`manifests/e5b/queries.log`, next to
`runs/`).

`admission` applies design 9.3's A-3 checks to each E5b arm, with the frozen script's functions: (1) a
smoke call accepted in the arm's fallback order; (2) at most 2% invalid planner calls in its canary;
(3) the canary's manipulation checks; (4) canary cost at most $0.060 per episode, compute included; (5)
not required, every E5b entry has temperature 0. E5b flies no Phase 0, so check 2 reads the canary
alone. An arm is not run if check 1 or 4 fails; otherwise it is descriptive whatever checks 2 and 3 give
(9.3: E5 was unblinded before E5b), and those two are reported with its results.

`analyse` is E5b's one pinned run: the frozen `lip stats --design v3` over E5's six manifests and the
three E5b arms'. It refuses to run until `docs/prereg/e5b-unblind.md` is on `origin/main` (fetch first)
and in this checkout, and then checks that
E5's arms, gates and analysis block equal E5's pinned output (`docs/results/freeze-v3-analysis.json`).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from lost_in_place import cli, freeze
from lost_in_place.llm.registry import get_model
from lost_in_place.wave import health
from lost_in_place.wave.aws import mask
from lost_in_place.wave.ledger import iso
from lost_in_place.wave.store import LocalStore, S3Store, Store

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = "t1"
N = 120
WAVE_OUT = Path("sweeps/e5b")
CANARY_OUT = Path("sweeps/e5b-canary")
WAVE_CONFIG = Path("src/lost_in_place/wave/e5b.yaml")
DOCKERFILE = Path("docker/Dockerfile.e5b")
UNBLIND = Path("docs/prereg/e5b-unblind.md")
E5_PINNED = Path("docs/results/freeze-v3-analysis.json")
E5_SWEEPS = (
    "scripted",
    "claude-sonnet-4-6",
    "claude-sonnet-5-5",
    "gpt-5.6-sol",
    "qwen3-4b-student",
    "qwen3-4b-zero-shot",
)
SECTION = "## E5b: three later arms (amendment of 2026-10-21)"
IMAGE_ROW = "E5b image digest"
SCRIPTED = "scripted"
# Each arm's registry entries, base entry first, then its pre-declared fallbacks (design 9.2's order).
ARMS: dict[str, tuple[str, ...]] = {
    "llama-3-3-70b": ("llama-3-3-70b",),
    "deepseek-v3-2": ("deepseek-v3-2", "deepseek-v3-2-fb1"),
    "mistral-large-3": ("mistral-large-3", "mistral-large-3-fb1"),
}
ENTRIES = tuple(e for entries in ARMS.values() for e in entries)
# Rows E5b changes by adding entries; every other row of the record must still match.
ADDED_TO = (freeze.MODELS_ROW, freeze.PROMPT_ROW_V3)


def _script(name: str) -> ModuleType:
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def wave(entries: dict[str, str], root: Path = ROOT) -> dict[str, str]:
    """`{file name: YAML text}` of the E5b wave sweeps: one per arm, each flying `entries[arm]`, and the
    scripted control."""
    gen = _script("e5_sweeps")
    out = {}
    for arm, entry in entries.items():
        if entry not in ARMS[arm]:
            raise ValueError(f"{arm} entry {entry!r}: use one of {', '.join(ARMS[arm])}")
        data = gen.llm_sweep(entry, TEMPLATE, N, arm)
        data["name"] = f"e5b-{arm}"
        episodes = gen.count(data, root)
        if episodes != gen.EXPECTED[N]["llm"]:
            raise AssertionError(f"{arm}: {episodes} episodes, E5 flew {gen.EXPECTED[N]['llm']} per LLM arm")
        header = [
            f"E5b wave (SPEC-v3 amendment of 2026-10-21): dexi/{entry} in E5's cells, levels and families.",
            f"Template {TEMPLATE}, N = {N}: {episodes} episodes. Written by scripts/e5b.py.",
        ]
        out[f"{arm}.yaml"] = gen.render(data, header)
    data = gen.scripted_sweep(TEMPLATE, N)
    data["name"] = f"e5b-{SCRIPTED}"
    episodes = gen.count(data, root)
    if episodes != gen.EXPECTED[N]["scripted"]:
        raise AssertionError(f"scripted: {episodes} episodes, E5 flew {gen.EXPECTED[N]['scripted']}")
    header = [
        "E5b wave (SPEC-v3 amendment of 2026-10-21): E5's scripted DEXI-cadence control, of which the",
        f"wave flies the S-1 gate cells. Template {TEMPLATE}, N = {N}: {episodes} episodes. Written by",
        "scripts/e5b.py.",
    ]
    out[f"{SCRIPTED}.yaml"] = gen.render(data, header)
    return out


def canary(root: Path = ROOT) -> dict[str, str]:
    """`{file name: YAML text}` of the E5b canary sweeps, one per entry, under E5's template and names."""
    gen = _script("e5_sweeps")
    out = {}
    for entry in ENTRIES:
        data = gen.canary_llm_sweep(entry, TEMPLATE)
        episodes = gen.count(data, root)
        if episodes != gen.CANARY_EXPECTED["llm"]:
            raise AssertionError(
                f"{entry}: {episodes} canary episodes, expected {gen.CANARY_EXPECTED['llm']}"
            )
        header = [
            f"E5b canary (SPEC-v3 amendment of 2026-10-21), template {TEMPLATE}: dexi/{entry} on k000-k005",
            f"at the 7 loud and 6 brief levels. {episodes} episodes. Written by scripts/e5b.py.",
        ]
        out[f"{TEMPLATE}-{entry}.yaml"] = gen.render(data, header)
    return out


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def frozen_registry_length(current: bytes, recorded: str) -> int | None:
    """The length of the prefix of `current`, ending at a line break, whose hash is `recorded`."""
    for end in (i + 1 for i, byte in enumerate(current) if byte == ord("\n")):
        if _sha(current[:end]) == recorded:
            return end
    return None


def flown_entries(root: Path = ROOT) -> tuple[list[str], dict[str, str]]:
    """(problems, `{arm: entry}`) from the written wave sweeps and config: each arm's sweep flies one
    of its entries, and its config arm names the same one."""
    problems: list[str] = []
    flown: dict[str, str] = {}
    for arm, entries in ARMS.items():
        path = root / WAVE_OUT / f"{arm}.yaml"
        if not path.is_file():
            continue
        cells = yaml.safe_load(path.read_text())["cells"]
        planners = {p["arm"] for cell in cells for p in cell["planners"]}
        found = [e for e in entries if planners == {f"dexi/{e}"}]
        if not found:
            problems.append(
                f"{path.relative_to(root)}: flies {sorted(planners)}, not one of {', '.join(entries)}"
            )
        else:
            flown[arm] = found[0]
    config = root / WAVE_CONFIG
    if config.is_file():
        arms = {a["name"]: a for a in yaml.safe_load(config.read_text())["arms"]}
        for arm, entry in flown.items():
            got = arms.get(arm, {})
            if got.get("sweep") != str(WAVE_OUT / f"{arm}.yaml") or got.get("model") != entry:
                problems.append(f"{WAVE_CONFIG}: arm {arm} must fly {WAVE_OUT / arm}.yaml with model {entry}")
    return problems, flown


def recorded_e5b(spec_text: str) -> dict[str, str]:
    """The values written in SPEC-v3's E5b record; a row whose value isn't in backticks (such as
    "recorded at launch") is left out."""
    lines = spec_text.split("\n")
    if SECTION not in lines:
        return {}
    start = lines.index(SECTION)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    found = {}
    for line in lines[start:end]:
        m = freeze._ROW.match(line)
        if m and m["value"].startswith("`") and m["value"].endswith("`"):
            found[m["artifact"]] = m["value"].strip("`")
    return found


def dockerfile_bases(text: str) -> list[str]:
    """The digest of every `FROM` line; a `FROM` without a digest counts as an empty one."""
    line = r"^FROM\s+\S+?(?:@(sha256:[0-9a-f]{64}))?(?:\s+AS\s+\S+)?\s*$"
    return [(m.group(1) or "") for m in re.finditer(line, text, re.M | re.I)]


def check(root: Path = ROOT, image_digest: str | None = None) -> tuple[list[str], dict[str, str]]:
    """(problems, E5b values). No problem means E5's record still describes E5 exactly, and the E5b
    record describes this checkout."""
    spec_text = (root / freeze.SPEC_V3).read_text()
    recorded = freeze.recorded_v3(spec_text)
    problems: list[str] = []
    values, missing = freeze.record_v3(root, None, freeze.adaptation_from_record(spec_text))
    problems += [f"{row}: missing ({reason})" for row, reason in missing.items()]
    problems += [
        m
        for m in freeze.mismatches_v3(spec_text, values)
        if not m.startswith(tuple(f"{r}:" for r in ADDED_TO))
    ]

    registry = (root / freeze.MODEL_REGISTRY).read_bytes()
    end = frozen_registry_length(registry, recorded[freeze.MODELS_ROW])
    if end is None:
        problems.append(f"{freeze.MODEL_REGISTRY}: the registry recorded at freeze-v3 is not a prefix of it")
    else:
        added = yaml.safe_load(registry[end:]) or {}
        if sorted(added) != sorted(ENTRIES):
            problems.append(
                f"{freeze.MODEL_REGISTRY}: entries after the frozen registry are {sorted(added)}, "
                f"not the E5b entries {sorted(ENTRIES)}"
            )

    bundle = freeze.prompt_bundle_v3(root)
    wire = bundle["render_grid"]["wire"]
    absent = [e for e in ENTRIES if e not in wire]
    if absent:
        problems.append(f"prompt bundle: no wire digest for {', '.join(absent)}")
    e5b_wire = {e: wire.pop(e) for e in ENTRIES if e in wire}
    without = freeze.sha256(freeze._canonical(bundle))
    if without != recorded[freeze.PROMPT_ROW_V3]:
        problems.append(
            f"prompt bundle without the E5b entries: {without}, recorded {recorded[freeze.PROMPT_ROW_V3]}"
        )

    flown_problems, flown = flown_entries(root)
    problems += flown_problems
    e5b = {
        "Model registry with the E5b entries": _sha(registry),
        **{f"Wire digest, `{e}`": d for e, d in e5b_wire.items()},
        **{f"Entry flown, `{arm}`": entry for arm, entry in flown.items()},
    }
    for out in (WAVE_OUT, CANARY_OUT):
        if sorted((root / out).glob("*.yaml")):
            e5b[f"`{out}/*.yaml` (sorted, concatenated)"] = freeze.scenario_set_hash(root / out)
    if (root / WAVE_CONFIG).is_file():
        e5b[f"Wave config (`{WAVE_CONFIG}`)"] = _sha((root / WAVE_CONFIG).read_bytes())
    dockerfile = root / DOCKERFILE
    if dockerfile.is_file():
        e5b[f"Image recipe (`{DOCKERFILE}`)"] = _sha(dockerfile.read_bytes())
        bases = dockerfile_bases(dockerfile.read_text())
        if bases != [recorded[freeze.IMAGE_ROW]]:
            problems.append(
                f"{DOCKERFILE}: builds on {bases}, not the frozen image {recorded[freeze.IMAGE_ROW]}"
            )
    else:
        problems.append(f"{DOCKERFILE}: missing")

    written = recorded_e5b(spec_text)
    for row, value in written.items():
        if row != IMAGE_ROW and e5b.get(row) != value:
            problems.append(f"E5b record: {row} is {value}, this checkout gives {e5b.get(row) or 'nothing'}")
    if image_digest is not None and written.get(IMAGE_ROW) != image_digest:
        problems.append(
            f"E5b record: {IMAGE_ROW} is {written.get(IMAGE_ROW) or 'not recorded'}, not {image_digest}"
        )
    return problems, e5b


def analyse(runs: str, out: Path, arms: tuple[str, ...] = tuple(ARMS), root: Path = ROOT) -> int:
    """E5b's pinned run, once `UNBLIND` is committed: the frozen `lip stats --design v3` over E5's
    manifests and `arms`', written to `<out>.json` and `<out>.md`. 3 while blinded; 1 if an E5 arm,
    the gates or the analysis block differ from E5's pinned output."""

    def absent(ref: str) -> bool:
        found = subprocess.run(["git", "cat-file", "-e", f"{ref}:{UNBLIND}"], cwd=root, capture_output=True)
        return found.returncode != 0

    missing = [ref for ref in ("HEAD", "origin/main") if absent(ref)]
    if missing:
        print(f"E5b is blinded until {UNBLIND} is on origin/main and in this checkout", file=sys.stderr)
        return 3
    manifests = f"{runs.rstrip('/').rsplit('/', 1)[0]}/manifests"
    names = [f"e5-{s}" for s in E5_SWEEPS] + [f"e5b-{arm}" for arm in arms]
    flags = [a for name in names for a in ("--manifest", f"{manifests}/{name}.json")]
    code = cli.main(["stats", "--design", "v3", runs, *flags, "--root", str(root), "--out", str(out)])
    if code != 0:
        return code
    new = json.loads(Path(f"{out}.json").read_text())
    old = json.loads((root / E5_PINNED).read_text())
    differ = [k for k in ("analysis", "gates") if new.get(k) != old[k]]
    differ += [f"arms.{a}" for a in old["arms"] if new["arms"].get(a) != old["arms"][a]]
    for key in differ:
        print(f"{key} differs from {E5_PINNED}", file=sys.stderr)
    return 1 if differ else 0


def log_canary_health(
    store: Store, who: str, query: dict[str, Any], result: dict[str, Any], when: datetime | None = None
) -> str:
    """Append the canary-health read to the E5b queries log, in the health report's line format, with
    the columns it read."""
    key = yaml.safe_load((ROOT / WAVE_CONFIG).read_text())["queries_log"]
    rows = list(result["arms"].values())
    line = {
        "at": iso(when or datetime.now(UTC)),
        "principal": who,
        "query": {k: mask(v) if isinstance(v, str) else v for k, v in sorted(query.items())},
        "columns": sorted({c for row in rows for c in row}),
        "rows": len(rows),
        "git_sha": health.git_sha(),
    }
    store.append(key, json.dumps(line, sort_keys=True) + "\n")
    return store.where(key)


def admission(smoke: dict[str, Any], canary_health: dict[str, Any]) -> dict[str, Any]:
    """Per arm: the entry its smoke call accepted, design 9.3's checks 1-4 and its status. `smoke` is
    `{arm: {"tried": [...]}}` as `e5_admission.py --smoke`; `canary_health` is `e5_admission.py
    canary-health` over the E5b canaries."""
    adm = _script("e5_admission")
    unknown = set(smoke) - set(ARMS)
    if unknown:
        raise SystemExit(f"smoke record: unknown arms {sorted(unknown)}; the E5b arms are {', '.join(ARMS)}")
    out: dict[str, Any] = {}
    for arm, entries in ARMS.items():
        entry, problems = adm.smoke_entry(smoke.get(arm, {}), entries)
        checks: dict[str, Any] = {
            "1_smoke_call": adm._check(entry is not None, accepted=entry, problems=problems)
        }
        can = canary_health["arms"].get(entry or "")
        if can:
            rate = adm._rate(can["invalid_calls"], can["planner_calls"])
            checks["2_invalid"] = adm._check(
                rate is not None and rate <= adm.INVALID_AT_MOST,
                canary_rate=rate,
                at_most=adm.INVALID_AT_MOST,
            )
            loud, brief = can["loud_manipulation_decision_ok"], can["brief_manipulation_ok"]
            checks["3_manipulation"] = adm._check(
                loud >= adm.LOUD_DECISION_OK and brief >= adm.BRIEF_OK,
                loud=f"{loud}/{can['loud_episodes']}",
                brief=f"{brief}/{can['brief_episodes']}",
                need=(
                    f"loud >= {adm.LOUD_DECISION_OK}/{adm.LOUD_CANARY}, "
                    f"brief >= {adm.BRIEF_OK}/{adm.BRIEF_CANARY}"
                ),
            )
            usd = can.get("usd")
            per_episode = usd / can["episodes"] if usd is not None and can["episodes"] else None
            checks["4_cost"] = adm._check(
                per_episode is not None and per_episode <= adm.USD_PER_EPISODE_AT_MOST,
                usd_per_episode=per_episode,
                at_most=adm.USD_PER_EPISODE_AT_MOST,
            )
        else:
            for name in ("2_invalid", "3_manipulation", "4_cost"):
                checks[name] = adm._check(False, missing="canary for the accepted entry")
        stochastic = entry is not None and get_model(entry).temperature is None
        checks["5_retest"] = adm._check(not stochastic, required=stochastic)
        run = checks["1_smoke_call"]["ok"] and checks["4_cost"]["ok"] and not stochastic
        out[arm] = {
            "entry": entry,
            "checks": checks,
            "status": adm.DESCRIPTIVE if run else adm.NOT_RUN,
            "usd_per_episode": checks["4_cost"].get("usd_per_episode"),
        }
    return out


def write(files: dict[str, str], out: Path, check_only: bool) -> int:
    """Write the files, or with `check_only` compare them with `out`: 1 on any difference. Files in
    `out` that aren't generated are stale."""
    present = {p.name for p in out.glob("*.yaml")} if out.is_dir() else set()
    stale = sorted(present - set(files))
    if check_only:
        changed = sorted(n for n, t in files.items() if not (out / n).is_file() or (out / n).read_text() != t)
        for name in changed + stale:
            print(f"{out / name}: {'not generated' if name in stale else 'differs from the generator'}")
        return 1 if changed or stale else 0
    out.mkdir(parents=True, exist_ok=True)
    for name in stale:
        (out / name).unlink()
    for name, text in files.items():
        (out / name).write_text(text)
        print(out / name)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sweeps", help="the E5b wave and canary sweeps")
    for arm, entries in ARMS.items():
        if len(entries) > 1:
            flag = f"--{arm.split('-', 1)[0]}"
            s.add_argument(flag, dest=arm, default=entries[0], choices=entries, help=f"the entry {arm} flies")
    s.add_argument(
        "--check", action="store_true", help="compare with sweeps/e5b{,-canary}/ instead of writing"
    )
    c = sub.add_parser(
        "check", help="the E5b preflight: E5's record still describes E5; prints the E5b values"
    )
    c.add_argument("--image-digest", help="sha256:<digest> the job definition runs; must be the recorded one")
    n = sub.add_parser(
        "analyse", help="E5b's pinned lip stats run, once docs/prereg/e5b-unblind.md is committed"
    )
    n.add_argument("runs", help="s3://<bucket>/runs")
    n.add_argument("--out", type=Path, required=True, help="writes <out>.json and <out>.md")
    n.add_argument(
        "--arm", action="append", choices=list(ARMS), help="the E5b arms flown (default: all three)"
    )
    h = sub.add_parser("canary-health", help="e5_admission.canary_health over the E5b canaries (reads only)")
    h.add_argument("runs_root", help="local mirror of runs/ or s3://bucket/runs")
    h.add_argument(
        "--usd", action="append", default=[], metavar="ENTRY=DOLLARS", help="metered, job time included"
    )
    a = sub.add_parser("admission", help="design 9.3's checks 1-4 per E5b arm, as JSON")
    a.add_argument("--smoke", type=Path, required=True)
    a.add_argument("--canary", type=Path, required=True, help="this script's canary-health output")
    for p in (h, a):
        p.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.cmd == "sweeps":
        flying = {arm: getattr(args, arm, ARMS[arm][0]) for arm in ARMS}
        failed = write(wave(flying), ROOT / WAVE_OUT, args.check)
        return failed | write(canary(), ROOT / CANARY_OUT, args.check)
    if args.cmd in ("canary-health", "admission"):
        if args.cmd == "canary-health":
            usd = {entry: float(value) for entry, _, value in (item.partition("=") for item in args.usd)}
            result = _script("e5_admission").canary_health(args.runs_root, TEMPLATE, usd, ENTRIES)
            if args.runs_root.startswith("s3://"):
                import boto3

                bucket = args.runs_root.removeprefix("s3://").split("/", 1)[0]
                store: Store = S3Store(boto3.client("s3"), bucket)
                who = health.principal(boto3.client("sts"))
            else:
                store, who = LocalStore(Path(args.runs_root).resolve().parent), "local"
            query = {"cmd": "canary-health", "runs_root": args.runs_root, "usd": args.usd}
            print(f"query logged to {log_canary_health(store, who, query, result)}", file=sys.stderr)
        else:
            result = admission(json.loads(args.smoke.read_text()), json.loads(args.canary.read_text()))
        text = json.dumps(result, indent=1, sort_keys=True) + "\n"
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text)
        print(text, end="")
        return 0
    if args.cmd == "analyse":
        return analyse(args.runs, args.out, tuple(args.arm or ARMS))
    problems, e5b = check(image_digest=args.image_digest)
    for problem in problems:
        print(problem, file=sys.stderr)
    print("| Artifact | Hash |\n|---|---|")
    for row, value in e5b.items():
        print(f"| {row} | `{value}` |")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
