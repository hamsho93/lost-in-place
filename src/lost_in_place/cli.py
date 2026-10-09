"""Command line: `lip worlds`, `lip run`, `lip analyze`, `lip doctor`."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from lost_in_place import __version__
from lost_in_place.config import Paths


def _cmd_worlds(args: argparse.Namespace) -> int:
    from lost_in_place.worlds import generate

    paths = Paths.from_env()
    out = args.out or paths.worlds_dir
    for world in generate(paths.gz_worlds, out, seed=args.seed):
        print(world)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from lost_in_place.episode import run_episode
    from lost_in_place.scenario import load_scenario

    scenario = load_scenario(args.scenario)
    seed = args.seed + int(os.environ.get("AWS_BATCH_JOB_ARRAY_INDEX", "0"))
    out = args.out or Path("runs") / f"{scenario.id}_s{seed}"
    result = run_episode(scenario, seed, out, instance=args.instance)
    summary = {"scenario": scenario.id, "seed": seed, "outcome": result.outcome, "failsafe": result.failsafe}
    print(json.dumps({**summary, "out": str(out)}))
    status = 0 if result.outcome == "completed" else 1
    if args.analyze and (out / "flight.ulg").exists():
        status = max(status, _analyze([out]))
    if args.upload:
        from lost_in_place.storage import upload_dir

        print(f"uploaded {upload_dir(out, args.upload)} files to {args.upload}")
    return status


def _analyze(run_dirs: list[Path]) -> int:
    from lost_in_place.metrics import analyze

    status = 0
    for run_dir in run_dirs:
        try:
            print(json.dumps(analyze(run_dir)))
        except (OSError, ValueError) as exc:
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
    run.add_argument("--instance", type=int, default=0, help="parallel slot (separate ports and partition)")
    run.add_argument("--out", type=Path)
    run.add_argument("--analyze", action="store_true", help="compute metrics after the episode")
    run.add_argument("--upload", metavar="S3_URI", help="upload the episode directory, e.g. s3://bucket/runs")
    run.set_defaults(func=_cmd_run)

    analyze = sub.add_parser("analyze", help="compute metrics for finished episodes")
    analyze.add_argument("run_dirs", type=Path, nargs="+")
    analyze.set_defaults(func=lambda a: _analyze(a.run_dirs))

    doctor = sub.add_parser("doctor", help="check that PX4 and Gazebo are where we expect")
    doctor.set_defaults(func=_cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code: int = args.func(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
