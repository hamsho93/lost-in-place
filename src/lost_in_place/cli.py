"""Command line: `lip worlds`, `lip run`, `lip analyze`, `lip manifest`, `lip run-manifest`, `lip report`,
`lip stats`, `lip label-sample`, `lip label-agree`, `lip freeze-record`, `lip doctor`, `lip models`,
`lip propose`, `lip e5 health`."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from lost_in_place import __version__
from lost_in_place.config import Paths

if TYPE_CHECKING:
    from lost_in_place.agent import AgentConfig


def _cmd_worlds(args: argparse.Namespace) -> int:
    from lost_in_place.worlds import generate

    paths = Paths.from_env()
    out = args.out or paths.worlds_dir
    for world in generate(paths.gz_worlds, out, seed=args.seed):
        print(world)
    return 0


def _fly(
    scenario_path: Path,
    seed: int,
    out: Path,
    agent: AgentConfig,
    *,
    instance: int | None,
    analyze: bool,
    upload: str | None,
    upload_name: str | None = None,
) -> int:
    """Fly, analyze and upload one episode. Exits non-zero only on a harness failure, which Batch
    retries; an analysis error never does, because a retry would re-roll a result. Nor does a harness
    failure whose score shows a violation (`manifest.is_result`)."""
    from lost_in_place.episode import run_episode
    from lost_in_place.manifest import is_result
    from lost_in_place.scenario import load_scenario
    from lost_in_place.slots import claim_slot

    scenario = load_scenario(scenario_path)
    with claim_slot(instance) as slot:
        result = run_episode(
            scenario, seed, out, instance=slot, agent=agent if scenario.mission is not None else None
        )
    summary = {"scenario": scenario.id, "seed": seed, "outcome": result.outcome, "failsafe": result.failsafe}
    print(json.dumps({**summary, "out": str(out)}))
    if analyze and (out / "flight.ulg").exists():
        try:
            _analyze([out])
        except Exception as exc:  # the episode stands; re-score it offline
            print(f"{out}: analysis failed: {exc!r}", file=sys.stderr)
    if upload:
        from lost_in_place.storage import attempt_label, upload_dir

        print(f"uploaded {out} to {upload_dir(out, upload, attempt_label(), upload_name)}")
    score_path = out / "score.json"
    score = json.loads(score_path.read_text()) if score_path.exists() else None
    return 0 if result.outcome == "completed" or is_result(result.outcome, score) else 1


def _cmd_run(args: argparse.Namespace) -> int:
    from lost_in_place.scenario import load_scenario

    seed = args.seed + int(os.environ.get("AWS_BATCH_JOB_ARRAY_INDEX", "0"))
    out = args.out or Path("runs") / f"{load_scenario(args.scenario).id}_s{seed}"
    return _fly(
        args.scenario,
        seed,
        out,
        _agent_config(args),
        instance=args.instance,
        analyze=args.analyze,
        upload=args.upload,
    )


def _agent_config(args: argparse.Namespace) -> AgentConfig:
    from lost_in_place.agent import AgentConfig

    options = dict(opt.split("=", 1) for opt in args.planner_opt)
    return AgentConfig(
        planner=args.planner,
        planner_options=options,
        validator=args.validator,
        observation=args.obs,
        latency_s=args.latency,
        pause=not args.no_pause,
    )


def _cmd_manifest(args: argparse.Namespace) -> int:
    from lost_in_place import manifest

    spec = manifest.SweepSpec.load(args.spec)
    entries = manifest.build(spec, args.root)
    text = manifest.dumps(spec, entries)
    out = args.out or Path("manifests") / f"{spec.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    target = str(out)
    if args.upload:
        import boto3

        from lost_in_place.storage import parse_s3_uri

        bucket, prefix = parse_s3_uri(args.upload)
        key = "/".join(p for p in (prefix, f"{spec.name}.json") if p)
        boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=text.encode())
        target = f"s3://{bucket}/{key}"
    cells = len({(e.scenario_id, e.cell) for e in entries})
    print(json.dumps({"manifest": target, "episodes": len(entries), "scenario_cells": cells}))
    for offset, size in manifest.array_chunks(len(entries)):
        print(
            f"aws batch submit-job --job-name {spec.name}-{offset} --job-queue <JobQueue> "
            f"--job-definition <SweepJobDefinition> --array-properties size={size} "
            f"--parameters manifest={target},offset={offset}"
        )
    return 0


def _cmd_run_manifest(args: argparse.Namespace) -> int:
    from lost_in_place import manifest
    from lost_in_place.agent import AgentConfig

    index = args.offset + (
        args.index if args.index is not None else int(os.environ.get("AWS_BATCH_JOB_ARRAY_INDEX", "0"))
    )
    document = manifest.read_document(args.manifest)
    entries = [manifest.Entry(**e) for e in document["entries"]]
    if not 0 <= index < len(entries):
        print(f"index {index} is outside the manifest ({len(entries)} entries)", file=sys.stderr)
        return 2
    entry = entries[index]
    upload = manifest.runs_root(args.upload, document) if args.upload else None
    print(json.dumps({"index": index, "key": entry.key, "runs": upload}))
    if upload and not args.force:
        from lost_in_place.storage import attempt_results

        attempts = attempt_results(upload, entry.key)
        if manifest.settled(attempts):
            print(json.dumps({"skipped": entry.key, "attempts": [o for o, _ in attempts]}))
            return 0
    agent = AgentConfig(
        planner=entry.planner,
        planner_options=entry.planner_options,
        validator=entry.validator,
        observation=entry.observation,
        latency_s=entry.latency_s,
    )
    return _fly(
        Path(entry.scenario),
        entry.seed,
        Path("runs") / entry.key,
        agent,
        instance=None,
        analyze=args.analyze,
        upload=upload,
        upload_name=entry.key,
    )


def _cmd_report(args: argparse.Namespace) -> int:
    from lost_in_place import report

    runs: str = args.runs
    keys = None
    if args.manifest:
        from lost_in_place import manifest

        document = manifest.read_document(args.manifest)
        keys = {manifest.Entry(**e).key for e in document["entries"]}
        if runs.startswith("s3://"):
            runs = manifest.runs_root(runs, document)
    attempts = report.s3_attempts(runs) if runs.startswith("s3://") else report.local_attempts(Path(runs))
    summary = report.summarize(attempts, keys)
    print(json.dumps(summary, indent=1) if args.json else report.markdown(summary))
    return 0


def _cmd_label_sample(args: argparse.Namespace) -> int:
    from lost_in_place import labels, report

    runs: str = args.runs
    keys = None
    if args.manifest:
        from lost_in_place import manifest

        document = manifest.read_document(args.manifest)
        keys = {manifest.Entry(**e).key for e in document["entries"]}
        if runs.startswith("s3://"):
            runs = manifest.runs_root(runs, document)
    attempts = report.s3_attempts(runs) if runs.startswith("s3://") else report.local_attempts(Path(runs))
    picked = labels.pick(attempts, args.fraction, args.seed, keys)
    if not picked:
        print("no scored episodes to sample", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    labels.sample(picked, labels.loader(runs), args.out, args.seed, with_plots=not args.no_plots)
    print(
        f"{len(picked)} episodes in {args.out}. Give the labeller {args.out / labels.BLIND}/ only, and"
        f" {labels.LABELLER_MODEL} the prompt in BRIEF.md; keep key.json."
    )
    return 0


def _cmd_label_agree(args: argparse.Namespace) -> int:
    from lost_in_place import labels

    result = labels.agree(labels.read_sheet(args.sheet.read_text()), json.loads(args.key.read_text()))
    print(json.dumps(result, indent=1) if args.json else labels.markdown(result))
    return 0


def _cmd_freeze_record(args: argparse.Namespace) -> int:
    from lost_in_place import freeze

    if args.freeze == freeze.Freeze.V3:
        return _freeze_record_v3(args)
    v3_only = [
        flag
        for flag, value in (
            ("--tag-message", args.tag_message),
            ("--template", args.template),
            ("--n", args.n),
            ("--arms", args.arms),
            ("--drop", args.drop),
        )
        if value
    ]
    if v3_only:
        print(f"{', '.join(v3_only)}: only with --freeze v3", file=sys.stderr)
        return 2
    spec = args.root / freeze.SPEC
    if args.analysis:
        return _pinned_script_record(args, spec, freeze.ANALYSIS_ROW)
    if args.e4_supplement:
        return _pinned_script_record(args, spec, freeze.E4_SUPPLEMENT_ROW)
    values = freeze.record(args.root, args.design, args.image_digest)
    if args.check:
        problems = freeze.mismatches(spec.read_text(), values)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    if args.write:
        spec.write_text(freeze.write_record(spec.read_text(), values))
    print("| Artifact | Hash |\n|---|---|")
    for row, value in values.items():
        print(f"| {row} | {f'`{value}`' if value else ''} |")
    return 0


def _pinned_script_record(args: argparse.Namespace, spec: Path, row: str) -> int:
    """A pinned script (the analysis script, the E4 supplement): print what the amendment-log entry
    names; `--write` adds its row; `--check` fails unless the row is there and matches."""
    from lost_in_place import freeze

    digest = freeze.script_hash(args.root, row)
    if args.check:
        problems = freeze.mismatches(spec.read_text(), {row: digest}, require=(row,))
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    if args.write:
        spec.write_text(freeze.write_pin(spec.read_text(), args.root, row))
        print(f"wrote {row} and refreshed '{freeze.ROWS[0]}' in {spec}", file=sys.stderr)
    try:
        sha = freeze.git_sha(args.root)
    except (subprocess.CalledProcessError, FileNotFoundError):
        sha = "(not a git checkout)"
    print(f"path: {freeze.PINNED_SCRIPTS[row]}\ngit SHA: {sha}\nSHA-256: {digest}")
    return 0


def _freeze_record_v3(args: argparse.Namespace) -> int:
    """SPEC-v3.md's record. `--write` needs the adaptation outcomes and the image digest and refuses
    a missing artifact or a dirty checkout; `--check` reads the outcomes from the record;
    `--tag-message` prints the `freeze-v3-pre` hash list; `--drop` marks recorded arms dropped. A
    missing artifact exits 1."""
    from lost_in_place import freeze

    if args.analysis or args.e4_supplement:
        print(
            "--analysis and --e4-supplement are freeze-v2 pins; v3's analysis row is in its record",
            file=sys.stderr,
        )
        return 2
    if args.tag_message:
        values, missing = freeze.tag_hashes(args.root, args.image_digest)
        for label, reason in missing.items():
            print(f"missing: {label}: {reason}", file=sys.stderr)
        print("freeze-v3-pre: the E5 pre-registration (docs/prereg/freeze-v3-design.md, section 2.1)\n")
        print("\n".join(f"- {label}: `{value}`" for label, value in values.items() if value))
        return 1 if missing else 0
    spec = args.root / freeze.SPEC_V3
    if not spec.is_file():
        print(f"no {spec}", file=sys.stderr)
        return 1
    given = [args.template, args.n, args.arms]
    if args.drop:
        if any(v is not None for v in given):
            print(
                "--drop reads the outcomes from the record; it takes no --template, --n or --arms",
                file=sys.stderr,
            )
            return 2
        return _drop_arms_v3(args.root, spec, args.drop)
    if any(v is not None for v in given) and None in given:
        print("--template, --n and --arms go together", file=sys.stderr)
        return 2
    try:
        if args.template is not None:
            adaptation = freeze.Adaptation(args.template, args.n, freeze.parse_arms(args.arms))
        elif args.write:
            print("--write needs --template, --n and --arms (the A-1 to A-5 outcomes)", file=sys.stderr)
            return 2
        else:
            adaptation = freeze.adaptation_from_record(spec.read_text())
    except ValueError as e:
        if args.check or args.template is not None:
            print(e, file=sys.stderr)
            return 1
        adaptation = None
    unknown = freeze.unknown_arms(args.root, adaptation) if adaptation is not None else []
    if unknown:
        print(f"not in {freeze.MODEL_REGISTRY}: {', '.join(unknown)}", file=sys.stderr)
        return 1
    if args.write and not args.image_digest:
        print("--write needs --image-digest", file=sys.stderr)
        return 2
    values, missing = freeze.record_v3(args.root, args.image_digest, adaptation, args.design)
    problems = [f"{row}: missing ({reason})" for row, reason in missing.items()]
    if args.check:
        problems += freeze.mismatches_v3(spec.read_text(), values)
        if not args.image_digest:
            print(f"{freeze.IMAGE_ROW} not compared: pass --image-digest", file=sys.stderr)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    if args.write:
        if values[freeze.GIT_ROW].endswith("-dirty"):
            problems.append(f"{freeze.GIT_ROW}: {values[freeze.GIT_ROW]}; --write needs a clean checkout")
        if problems:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 1
        spec.write_text(freeze.write_record_v3(spec.read_text(), values))
    for problem in problems:
        print(problem, file=sys.stderr)
    print("| Artifact | Hash |\n|---|---|")
    for row, value in values.items():
        shown = f"`{value}`" if value else ("(missing)" if row in missing else "")
        print(f"| {row} | {shown} |")
    return 1 if problems else 0


def _drop_arms_v3(root: Path, spec: Path, arms: str) -> int:
    """Mark recorded confirmatory arms dropped (a deferred arm whose month was not approved, design
    section 10.4): only the record's dropped-arms row changes; the arms stay in m, as tagged."""
    from lost_in_place import freeze

    text = spec.read_text()
    try:
        adaptation = freeze.adaptation_from_record(text).drop(freeze.parse_arms(arms))
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    unknown = freeze.unknown_arms(root, adaptation)
    if unknown:
        print(f"not in {freeze.MODEL_REGISTRY}: {', '.join(unknown)}", file=sys.stderr)
        return 1
    values = adaptation.values()
    spec.write_text(freeze.write_record_v3(text, freeze.recorded_v3(text) | values))
    print(
        f"{freeze.DROPPED_ROW}: {values[freeze.DROPPED_ROW]}; {freeze.M_ROW} stays {adaptation.m}",
        file=sys.stderr,
    )
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    if args.design == "v3":
        return _cmd_stats_v3(args)
    if args.gates or args.canary or args.n is not None:
        print("--gates, --canary and --n go with --design v3", file=sys.stderr)
        return 1
    from lost_in_place import stats

    attempts, keys, inputs = stats.load_attempts(args.runs, args.manifest)
    result = stats.analyse(
        attempts,
        keys,
        resamples=args.resamples,
        seed=args.seed,
        inputs=inputs,
        pin=stats.pin_status(args.root),
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.with_suffix(".json").write_text(stats.dumps(result))
        args.out.with_suffix(".md").write_text(stats.markdown(result))
        print(f"wrote {args.out.with_suffix('.json')} and {args.out.with_suffix('.md')}")
    else:
        print(stats.dumps(result) if args.json else stats.markdown(result), end="")
    if result["analysis"].get("pinned") is False:
        print(
            "warning: this analysis script is not pinned in SPEC.md; do not unblind results with it",
            file=sys.stderr,
        )
    return 0


def _cmd_stats_gates(args: argparse.Namespace) -> int:
    from lost_in_place import stats_v3

    if args.out or (args.canary and args.n is not None):
        print("--gates prints its rows; it takes neither --out nor, with --canary, --n", file=sys.stderr)
        return 1
    try:
        n = args.n if args.n is not None else stats_v3.recorded_n(args.root)
        result = stats_v3.gates_check(args.runs, args.manifest, n, canary=args.canary)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(
        json.dumps(result, indent=1) if args.json else stats_v3.gates_markdown(result),
        end="\n" if args.json else "",
    )
    return 0 if result["pass"] else 2


def _cmd_stats_v3(args: argparse.Namespace) -> int:
    from lost_in_place import stats_v3

    if args.gates:
        return _cmd_stats_gates(args)
    if args.canary or args.n is not None:
        print("--canary and --n go with --gates", file=sys.stderr)
        return 1
    status = stats_v3.gate(args.root)
    if not status["open"]:
        print(stats_v3.refusal(status), file=sys.stderr)
        return 3
    try:
        design = stats_v3.read_design((args.root / stats_v3.SPEC_V3).read_text())
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    try:
        data, inputs = stats_v3.load(args.runs, args.manifest)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    for cell, count in sorted(data.off_ladder.items()):
        print(f"excluded {count} episodes off the ladder: {cell}", file=sys.stderr)
    try:
        result = stats_v3.analyse(
            data,
            design,
            resamples=args.resamples,
            seed=args.seed,
            inputs=inputs,
            pin={"sha256": status["sha256"], "pinned": True},
        )
    except stats_v3.Unsettled as exc:
        print(f"refusing to print E5 outcomes: {exc}", file=sys.stderr)
        return 3
    for arm, count in result["inputs"]["incomplete_episodes"].items():
        print(
            f"{arm} has {count} incomplete episodes (attempted, no result, fewer than 3 attempts)",
            file=sys.stderr,
        )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.with_suffix(".json").write_text(stats_v3.dumps(result))
        args.out.with_suffix(".md").write_text(stats_v3.markdown(result))
        print(f"wrote {args.out.with_suffix('.json')} and {args.out.with_suffix('.md')}")
    else:
        print(stats_v3.dumps(result) if args.json else stats_v3.markdown(result), end="")
    return 0


def _analyze(run_dirs: list[Path]) -> int:
    """Metrics for every episode, and the section 7 score for agent episodes."""
    from lost_in_place.metrics import analyze
    from lost_in_place.scoring import score_run

    status = 0
    for run_dir in run_dirs:
        try:
            print(json.dumps(analyze(run_dir)))
            if json.loads((run_dir / "episode.json").read_text()).get("decisions"):
                print(json.dumps(score_run(run_dir)))
        except (OSError, ValueError, KeyError) as exc:
            print(f"{run_dir}: {exc}", file=sys.stderr)
            status = 1
    return status


def _cmd_doctor(_: argparse.Namespace) -> int:
    paths = Paths.from_env()
    ok = True
    for p in paths.missing():
        print(f"missing: {p}")
        ok = False
    if shutil.which("gz") is None:
        print("missing: gz (install Gazebo Harmonic)")
        ok = False
    else:
        version = subprocess.run(["gz", "sim", "--versions"], capture_output=True, text=True).stdout.strip()
        print(f"gz-sim {version}")
    print(f"PX4 build: {paths.build_dir}")
    print(f"worlds: {paths.worlds_dir}")
    print("ok" if ok else "not ready")
    return 0 if ok else 1


def _cmd_models(_: argparse.Namespace) -> int:
    from lost_in_place.llm.registry import load_models

    for spec in load_models().values():
        print(f"{spec.name}\t{spec.provider}\t{spec.model_id}")
    return 0


def _cmd_propose(args: argparse.Namespace) -> int:
    import asyncio
    from dataclasses import asdict

    from lost_in_place.llm.dexi import DEXI_DRIVER, DexiEnvelope, DexiPlanner
    from lost_in_place.llm.planner import LLMPlanner
    from lost_in_place.llm.prompt import InvalidProposal
    from lost_in_place.llm.registry import get_model
    from lost_in_place.planning import EstimatorHealth, Observation

    # "degraded" is the silent case: PX4 still reports a valid, precise position while flow is gone.
    sample_health = {
        "none": None,
        "healthy": EstimatorHealth(True, 0.05, 180, innovation_check_failing=False, heading_reference=False),
        "degraded": EstimatorHealth(True, 0.05, 0, innovation_check_failing=False, heading_reference=False),
    }
    observation = Observation(
        goal=args.goal,
        north_m=0.0,
        east_m=0.0,
        altitude_m=1.5,
        heading_deg=0.0,
        battery_pct=90.0,
        history=("take_off altitude_m=1.5: done",),
        health=sample_health[args.health],
    )
    spec = get_model(args.model)
    # The settings this call sends, so a smoke call's log shows which registry entry was tried.
    print(json.dumps({"model": spec.model_dump(exclude={"notes"}), "planner": args.planner}), file=sys.stderr)
    planner: LLMPlanner | DexiPlanner
    if args.planner == "dexi":
        envelope = DexiEnvelope(max_alt_m=2.0, geofence_m=5.5)
        planner = DexiPlanner(spec, envelope, 1.5, prompt_version=args.prompt, driver=DEXI_DRIVER)
    else:
        planner = LLMPlanner(spec)
    status = 0
    try:
        asyncio.run(planner.propose(observation))
    except InvalidProposal:
        status = 1
    print(json.dumps(asdict(planner.decisions[-1]), indent=1))
    return status


def _cmd_families(args: argparse.Namespace) -> int:
    from lost_in_place.families import generate

    if args.e5:
        return _families_e5(args)
    if args.count is None or args.check:
        print("lip families: --count is required, and --check goes with --e5", file=sys.stderr)
        return 2
    for i in range(args.start, args.start + args.count):
        family = generate(
            f"{args.prefix or 'g'}{i:02d}",
            0 if args.seed is None else args.seed,
            benign=args.benign,
            loud_anchored=args.loud_anchor,
        )
        for path in family.write(args.out or Path("scenarios/e1")):
            print(path)
    return 0


def _families_e5(args: argparse.Namespace) -> int:
    import collections
    import tempfile

    from lost_in_place import families

    fixed = {"--count": args.count, "--start": args.start or None, "--prefix": args.prefix}
    fixed |= {"--benign": args.benign or None, "--loud-anchor": args.loud_anchor or None}
    if given := [flag for flag, value in fixed.items() if value is not None]:
        print(f"lip families: --e5 draws a fixed set; drop {', '.join(given)}", file=sys.stderr)
        return 2
    out: Path = args.out or Path("scenarios/e5")
    draw = families.draw_e5(args.prior, families.E5_SEED if args.seed is None else args.seed)
    strata = collections.Counter(families.wording_stratum(f.scenarios()["healthy"]) for f in draw.families)
    n = len(draw.families)
    for template in families.TEMPLATES:
        shares = ", ".join(f"{k} {strata[k]}/{n} ({strata[k] / n:.1%})" for k in sorted(strata, key=str))
        print(f"{template}: {n} families, {len(draw.canaries)} canaries, {n} timed; strata {shares}")
    skipped = {k: v for k, v in draw.duplicates.items() if v}
    print(f"duplicates rejected: {sum(skipped.values())} {skipped}")
    if not args.check:
        families.write_e5(draw, out)
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp)
        families.write_e5(draw, fresh)
        want = {p.relative_to(fresh): p.read_bytes() for p in fresh.rglob("*.yaml")}
        have = {p.relative_to(out): p.read_bytes() for p in out.rglob("*.yaml")}
    bad = sorted(str(p) for p in want.keys() | have.keys() if want.get(p) != have.get(p))
    for path in bad:
        print(f"differs, missing or extra: {out / path}", file=sys.stderr)
    return 1 if bad else 0


def _cmd_dryrun(args: argparse.Namespace) -> int:
    import asyncio
    from dataclasses import asdict

    from lost_in_place import dryrun, wording
    from lost_in_place.llm.clients import make_client
    from lost_in_place.llm.registry import get_model

    spec = get_model(args.model)
    episodes = [(t, s) for t in args.templates for s in dryrun.wording_families(args.scenarios, t)]
    cost = wording.dryrun_cost(len(episodes)) if spec.provider == "bedrock_imported" else None
    print(
        f"dry run: {args.model} ({spec.provider}), {len(episodes)} episodes"
        f" ({', '.join(args.templates)} x {len(episodes) // len(args.templates)} families),"
        f" prompt {args.prompt}, {args.obs}, driver dexi, validator none, latency 0"
    )
    if cost is not None:
        print(f"  about {cost['calls']} calls; {cost['copy_minutes']} copy-minutes -> ${cost['usd']}")
    if not args.live:
        print("  plan only: add --live to call the model (it costs money)")
        return 0

    def progress(run: dryrun.DryRun) -> None:
        state = run.harness_error or ("success" if run.success else run.end)
        print(f"  {run.template} {run.family}: {state}", flush=True)

    runs = asyncio.run(
        dryrun.run_all(
            episodes, spec, lambda: make_client(spec), args.prompt, args.obs, args.jobs, progress=progress
        )
    )
    summary = dryrun.summarise(runs)
    doc = {
        "model": args.model,
        "prompt": args.prompt,
        "observation": args.obs,
        "driver": "dexi",
        "validator": "none",
        "latency_s": 0.0,
        "summary": summary,
        "episodes": [asdict(r) for r in runs],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=1) + "\n")
    print(json.dumps(summary))
    print(args.out)
    return 1 if any(row["harness_error"] for row in summary.values()) else 0


def _instance_arg(value: str) -> int | None:
    if value == "auto":
        return None
    try:
        slot = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a slot number or 'auto', got {value!r}") from None
    if slot < 0:
        raise argparse.ArgumentTypeError("slot numbers start at 0")
    return slot


def build_parser() -> argparse.ArgumentParser:
    from lost_in_place.planning import OBSERVATION_LEVELS

    parser = argparse.ArgumentParser(prog="lip", description="Lost in Place benchmark runner")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    worlds = sub.add_parser("worlds", help="generate the floor-texture worlds")
    worlds.add_argument("--out", type=Path, help="output directory (default: $LIP_WORLDS_DIR)")
    worlds.add_argument("--seed", type=int, default=0)
    worlds.set_defaults(func=_cmd_worlds)

    fam = sub.add_parser(
        "families", help="generate scenario families (four matched states each; five with --benign)"
    )
    fam.add_argument("--count", type=int, help="how many families (required without --e5)")
    fam.add_argument("--start", type=int, default=0, help="number of the first family (default: 0)")
    fam.add_argument("--prefix", help="family names are <prefix><number> (default: g)")
    fam.add_argument(
        "--seed",
        type=int,
        help="the same seed and names give the same families (default: 0; 7 with --e5)",
    )
    fam.add_argument("--out", type=Path, help="default: scenarios/e1; scenarios/e5 with --e5")
    fam.add_argument(
        "--benign",
        action="store_true",
        help="E2 variants: a safe second leg, a healthy control and a brief-flow-dropout control",
    )
    fam.add_argument(
        "--loud-anchor",
        action="store_true",
        help="start the loud fault when the estimate is 2 m from the first move's target"
        " (scenarios/smoke-audit-fixes), not 2.5 s into the move (E1, E2)",
    )
    fam.add_argument(
        "--e5",
        action="store_true",
        help="draw E5's fixed set (c000-c159, canaries k000-k005, deduplicated) and write it under"
        " both templates to <out>/{t0,t1}/{families,canary,timed}; prints the wording strata",
    )
    fam.add_argument(
        "--prior",
        type=Path,
        default=Path("scenarios"),
        help="with --e5: the directory holding the sets E5 must not repeat (default: scenarios)",
    )
    fam.add_argument(
        "--check",
        action="store_true",
        help="with --e5: compare with <out> instead of writing; exit 1 if any differ",
    )
    fam.set_defaults(func=_cmd_families)

    dry = sub.add_parser(
        "dryrun",
        help="fly a model through DEXI's prompt against a kinematic double, for the E5 wording check"
        " (b00-b59 re-worded plus k000-k005, per template); plans only, unless --live",
    )
    dry.add_argument("model", help="a model in llm/models.yaml (the wording check flies qwen3-4b-student)")
    dry.add_argument("--templates", nargs="+", choices=("t0", "t1"), default=["t0", "t1"])
    dry.add_argument("--prompt", choices=("dexi-v2", "dexi-9b2e7a7"), default="dexi-v2")
    dry.add_argument("--obs", choices=list(OBSERVATION_LEVELS), default="O0")
    dry.add_argument("--jobs", type=int, default=8, help="episodes in flight (default: 8)")
    dry.add_argument("--scenarios", type=Path, default=Path("scenarios"), help="default: scenarios")
    dry.add_argument("--out", type=Path, default=Path("runs/wording-check/dryrun.json"))
    dry.add_argument(
        "--live", action="store_true", help="call the model; without it, print the plan and cost"
    )
    dry.set_defaults(func=_cmd_dryrun)

    run = sub.add_parser("run", help="fly one scenario")
    run.add_argument("scenario", type=Path)
    run.add_argument(
        "--seed",
        type=int,
        default=0,
        help="spawn-pose seed when the scenario has none; AWS_BATCH_JOB_ARRAY_INDEX is added when set",
    )
    run.add_argument(
        "--instance",
        type=_instance_arg,
        default=None,
        metavar="N|auto",
        help="parallel slot (separate ports and partition); default: the lowest slot free on this host",
    )
    run.add_argument("--out", type=Path)
    run.add_argument("--analyze", action="store_true", help="compute metrics after the episode")
    run.add_argument("--upload", metavar="S3_URI", help="upload the episode directory, e.g. s3://bucket/runs")
    agent = run.add_argument_group("agent episodes (scenarios with a mission)")
    agent.add_argument("--planner", default="scripted-naive", help="planner arm (default: scripted-naive)")
    agent.add_argument(
        "--planner-opt",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="planner option, e.g. model=...",
    )
    agent.add_argument(
        "--validator",
        default="none",
        choices=["none", "geometric", "estimator_health", "oracle"],
        help="validator arm (default: none)",
    )
    agent.add_argument(
        "--obs", choices=list(OBSERVATION_LEVELS), default="O0", help="observation level (default: O0)"
    )
    agent.add_argument(
        "--latency", type=float, default=2.0, help="simulation seconds charged per decision (default: 2)"
    )
    agent.add_argument(
        "--no-pause", action="store_true", help="keep the sim running while the planner thinks"
    )
    run.set_defaults(func=_cmd_run)

    analyze = sub.add_parser(
        "analyze", help="compute metrics (and scores, for agent episodes) for finished episodes"
    )
    analyze.add_argument("run_dirs", type=Path, nargs="+")
    analyze.set_defaults(func=lambda a: _analyze(a.run_dirs))

    man = sub.add_parser("manifest", help="expand a sweep spec into a shuffled episode manifest")
    man.add_argument("spec", type=Path, help="sweep YAML, e.g. sweeps/e1-pilot.yaml")
    man.add_argument("--root", type=Path, default=Path(), help="repo root the scenario globs are under")
    man.add_argument("--out", type=Path, help="default: manifests/<name>.json")
    man.add_argument("--upload", metavar="S3_URI", help="also put it here, e.g. s3://bucket/manifests")
    man.set_defaults(func=_cmd_manifest)

    rm = sub.add_parser("run-manifest", help="fly one manifest entry (a Batch array child)")
    rm.add_argument("manifest", help="manifest path or s3:// URI")
    rm.add_argument("--offset", type=int, default=0, help="added to the index (arrays hold 10,000)")
    rm.add_argument("--index", type=int, help="default: AWS_BATCH_JOB_ARRAY_INDEX")
    rm.add_argument("--analyze", action="store_true", help="compute metrics and scores after the episode")
    rm.add_argument(
        "--upload",
        metavar="S3_URI",
        help="runs root, e.g. s3://bucket/runs; results go under <root>/<sweep name>",
    )
    rm.add_argument("--force", action="store_true", help="fly even if the episode already has a result there")
    rm.set_defaults(func=_cmd_run_manifest)

    rep = sub.add_parser("report", help="per-cell rates with Wilson 95%% intervals")
    rep.add_argument(
        "runs",
        help="runs root: a local directory, or s3://bucket/runs (with --manifest, plus its sweep's prefix)",
    )
    rep.add_argument("--manifest", help="only this manifest's episodes, and count those not started")
    rep.add_argument("--json", action="store_true", help="full summary as JSON instead of a table")
    rep.set_defaults(func=_cmd_report)

    st = sub.add_parser(
        "stats",
        help="the pre-registered analysis: family bootstrap intervals, Holm over the primaries, verdicts",
    )
    st.add_argument(
        "runs",
        nargs="+",
        help="runs roots: local directories, or s3://bucket/runs (with --manifest, plus each sweep's prefix)",
    )
    st.add_argument(
        "--manifest",
        action="append",
        default=[],
        help="only these manifests' episodes; repeat it for E2's two sweeps",
    )
    st.add_argument(
        "--resamples", type=int, default=10_000, help="family bootstrap resamples (default: 10000)"
    )
    st.add_argument("--seed", type=int, default=0, help="bootstrap seed (default: 0)")
    st.add_argument(
        "--root", type=Path, default=Path(), help="repo root with SPEC.md, to report the script's pin"
    )
    st.add_argument("--json", action="store_true", help="the full result as JSON instead of markdown")
    st.add_argument("--out", type=Path, help="write <out>.json and <out>.md instead of printing")
    st.add_argument(
        "--design",
        choices=["v2", "v3"],
        default="v2",
        help="v2: freeze-v2's analysis (stats.py, default); v3: E5's (stats_v3.py), which refuses to run"
        " until SPEC-v3.md pins it and docs/prereg/freeze-v3-signoff.md and"
        " docs/prereg/freeze-v3-unblind.md exist",
    )
    st.add_argument(
        "--gates",
        action="store_true",
        help="with --design v3: only the scripted control's section 6 gates (S-1), on scripted arms only;"
        " exit 0 all pass, 2 a gate failed, 1 bad input or incomplete cells",
    )
    st.add_argument("--canary", action="store_true", help="with --gates: the canary version, on k000-k005")
    st.add_argument(
        "--n", type=int, help="with --gates: families per cell (default: SPEC-v3.md's Families (N))"
    )
    st.set_defaults(func=_cmd_stats)

    ls = sub.add_parser(
        "label-sample", help="a blind hand-label sample of scored episodes, to check the scorer"
    )
    ls.add_argument("runs", help="runs root, as for `lip report`")
    ls.add_argument("--manifest", help="only this manifest's episodes")
    ls.add_argument("--fraction", type=float, default=0.1, help="share of each cell (default: 0.1)")
    ls.add_argument("--seed", type=int, default=0)
    ls.add_argument("--out", type=Path, required=True, help="directory for the packets, sheet and key")
    ls.add_argument("--no-plots", action="store_true", help="skip the plots (no matplotlib needed)")
    ls.set_defaults(func=_cmd_label_sample)

    la = sub.add_parser("label-agree", help="the hand labels' agreement with the scorer")
    la.add_argument("sheet", type=Path, help="the labeller's sheet.csv")
    la.add_argument("key", type=Path, help="key.json from `lip label-sample`")
    la.add_argument("--json", action="store_true")
    la.set_defaults(func=_cmd_label_agree)

    fr = sub.add_parser("freeze-record", help="SHA-256 of each artifact in SPEC's freeze record")
    fr.add_argument("--root", type=Path, default=Path(), help="repo root (default: .)")
    fr.add_argument(
        "--freeze",
        choices=["v2", "v3"],
        default="v2",
        help="v2: SPEC.md (E1-E4, the default); v3: SPEC-v3.md (E5)",
    )
    fr.add_argument(
        "--design",
        type=Path,
        help="the experiment design document (v3 default: docs/prereg/freeze-v3-design.md under --root)",
    )
    fr.add_argument("--image-digest", help="the sweep image's digest, sha256:...")
    pin = fr.add_mutually_exclusive_group()
    pin.add_argument(
        "--analysis",
        action="store_true",
        help="the analysis script's pin only: print its path, git SHA and SHA-256; with --write, add or"
        " update its row (and this file's hash); with --check, fail unless the row is there and matches",
    )
    pin.add_argument(
        "--e4-supplement",
        action="store_true",
        help="the same, for the E4 supplement script (scripts/e4_supplement.py)",
    )
    mode = fr.add_mutually_exclusive_group()
    mode.add_argument(
        "--write", action="store_true", help="fill in SPEC.md's (or SPEC-v3.md's) freeze record"
    )
    mode.add_argument(
        "--check", action="store_true", help="fail if SPEC.md's (or SPEC-v3.md's) record no longer matches"
    )
    mode.add_argument(
        "--tag-message",
        action="store_true",
        help="v3: print the freeze-v3-pre tag message's SHA-256 list (design section 2.1); exit 1 if any"
        " artifact is missing",
    )
    mode.add_argument(
        "--drop",
        metavar="ARMS",
        help="v3: mark recorded confirmatory arms dropped, comma-separated (a deferred month not approved,"
        " design section 10.4); writes only the Dropped arms row, and m stays as tagged",
    )
    v3 = fr.add_argument_group("freeze-v3 adaptation outcomes (with --freeze v3; required by --write)")
    v3.add_argument("--template", choices=["t0", "t1"], help="the wording template chosen by A-1")
    v3.add_argument("--n", type=int, choices=[120, 160], help="families per cell, by A-2")
    v3.add_argument(
        "--arms",
        help="the 1 to 5 confirmatory arms (A-3, A-4), comma-separated registry model names: the base"
        " entry or the one fallback entry it flies, e.g. claude-sonnet-4-6,qwen3-4b-student,"
        "qwen3-4b-zero-shot,gpt-5.6-sol,claude-sonnet-5-5; m = 4 x their number (up to 20)",
    )
    fr.set_defaults(func=_cmd_freeze_record)

    doctor = sub.add_parser("doctor", help="check that PX4 and Gazebo are where we expect")
    doctor.set_defaults(func=_cmd_doctor)

    models = sub.add_parser("models", help="list the planner models in models.yaml")
    models.set_defaults(func=_cmd_models)

    propose = sub.add_parser(
        "propose", help="ask a model for one decision on a sample observation (no simulator; checks access)"
    )
    propose.add_argument("model", help="a name from `lip models`")
    propose.add_argument("--goal", default="Hover over the take-off point for a minute, then land.")
    propose.add_argument(
        "--health",
        choices=["none", "healthy", "degraded"],
        default="none",
        help="estimator-health block: none (as deployed) or a sample healthy/degraded one",
    )
    propose.add_argument(
        "--planner",
        choices=["llm", "dexi"],
        default="llm",
        help="the prompt and tools: the generic LLM planner's, or DEXI's (driver dexi, validator none)",
    )
    propose.add_argument(
        "--prompt", default="dexi-v2", help="with --planner dexi: the DEXI prompt version (default dexi-v2)"
    )
    propose.set_defaults(func=_cmd_propose)

    from lost_in_place.wave.cli import add_e5_parser

    add_e5_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code: int = args.func(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
