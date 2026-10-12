"""Command line: `lip worlds`, `lip run`, `lip analyze`, `lip manifest`, `lip run-manifest`, `lip report`,
`lip doctor`, `lip models`, `lip propose`."""

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
    retries; an analysis error never does, because a retry would re-roll a result."""
    from lost_in_place.episode import run_episode
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
    return 0 if result.outcome == "completed" else 1


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
    entries = manifest.read(args.manifest)
    if not 0 <= index < len(entries):
        print(f"index {index} is outside the manifest ({len(entries)} entries)", file=sys.stderr)
        return 2
    entry = entries[index]
    print(json.dumps({"index": index, "key": entry.key}))
    if args.upload and not args.force:
        from lost_in_place.storage import attempt_outcomes

        outcomes = attempt_outcomes(args.upload, entry.key)
        if manifest.settled(outcomes):
            print(json.dumps({"skipped": entry.key, "attempts": outcomes}))
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
        upload=args.upload,
        upload_name=entry.key,
    )


def _cmd_report(args: argparse.Namespace) -> int:
    from lost_in_place import report

    runs: str = args.runs
    attempts = report.s3_attempts(runs) if runs.startswith("s3://") else report.local_attempts(Path(runs))
    summary = report.summarize(attempts)
    print(json.dumps(summary, indent=1) if args.json else report.markdown(summary))
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
    planner = LLMPlanner(get_model(args.model))
    status = 0
    try:
        asyncio.run(planner.propose(observation))
    except InvalidProposal:
        status = 1
    print(json.dumps(asdict(planner.decisions[-1]), indent=1))
    return status


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
    parser = argparse.ArgumentParser(prog="lip", description="Lost in Place benchmark runner")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    worlds = sub.add_parser("worlds", help="generate the floor-texture worlds")
    worlds.add_argument("--out", type=Path, help="output directory (default: $LIP_WORLDS_DIR)")
    worlds.add_argument("--seed", type=int, default=0)
    worlds.set_defaults(func=_cmd_worlds)

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
    agent.add_argument("--obs", choices=["O0", "O1"], default="O0", help="observation level (default: O0)")
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
    rm.add_argument("--upload", metavar="S3_URI", help="runs root, e.g. s3://bucket/runs")
    rm.add_argument("--force", action="store_true", help="fly even if the episode already has a result there")
    rm.set_defaults(func=_cmd_run_manifest)

    rep = sub.add_parser("report", help="per-cell rates with Wilson 95%% intervals")
    rep.add_argument("runs", help="runs root: a local directory or s3://bucket/runs")
    rep.add_argument("--json", action="store_true", help="full summary as JSON instead of a table")
    rep.set_defaults(func=_cmd_report)

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
    propose.set_defaults(func=_cmd_propose)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code: int = args.func(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
