"""Command lines for the wave: `scripts/e5_submit.py`, `scripts/e5_meter.py` and `lip e5 health`.

Each finds the stack's resources from CloudFormation (or the `LIP_*` environment overrides) and
runs under OperatorRole unless `--as-caller` is given. `--dry-run` makes every AWS client read-only
and keeps the ledger, the queries log and any command in a local directory.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from lost_in_place.wave import config, health, meter
from lost_in_place.wave.aws import Clients, Stack, boto3_factory, discover, mask, utcnow
from lost_in_place.wave.config import Wave
from lost_in_place.wave.episodes import LocalRuns, Runs, S3Runs
from lost_in_place.wave.ledger import Ledger, iso
from lost_in_place.wave.pinger import Pinger, invoke_ping
from lost_in_place.wave.store import LocalStore, Overlay, S3Store, Store
from lost_in_place.wave.submit import PreflightFailed, Submitter

Factory = Callable[[str], Any]
DEFAULT_LOCAL_DIR = Path(".e5-dryrun")


@dataclass
class Context:
    wave: Wave
    stack: Stack
    clients: Clients
    store: Store
    dry_run: bool

    def ledger(self) -> Ledger:
        return Ledger(self.store, self.wave.ledger_prefix)

    def submitter(self, root: Path, runs: Callable[[str], Runs] | None = None, **kwargs: Any) -> Submitter:
        return Submitter(
            self.wave,
            self.clients,
            self.stack,
            self.store,
            root=root,
            dry_run=self.dry_run,
            runs=runs,
            **kwargs,
        )


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, help="wave config (default: the packaged E5 wave)")
    parser.add_argument("--region", help="AWS region (default: AWS_REGION, AWS_DEFAULT_REGION or us-east-1)")
    parser.add_argument(
        "--as-caller", action="store_true", help="use the caller's credentials, not OperatorRole"
    )
    parser.add_argument("--dry-run", action="store_true", help="read-only AWS; ledger and logs stay local")
    parser.add_argument(
        "--local-dir", type=Path, default=DEFAULT_LOCAL_DIR, help="where a dry run keeps its ledger and logs"
    )
    parser.add_argument("--root", type=Path, default=Path(), help="repository root (sweeps, scenarios)")


def context(
    args: argparse.Namespace, factory: Factory | None = None, env: dict[str, str] | None = None
) -> Context:
    env = dict(os.environ) if env is None else env
    wave = config.load(args.config)
    region = args.region or env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-east-1"
    base = factory or boto3_factory(region)
    stack = discover(
        Clients(base, read_only=True)("cloudformation"), wave.stack, wave.job_definition_output, env
    )
    operator = (
        base if factory is not None or args.as_caller else boto3_factory(region, stack.operator_role_arn)
    )
    clients = Clients(operator, read_only=args.dry_run)
    s3 = S3Store(clients("s3"), stack.bucket)
    store: Store = Overlay(s3, LocalStore(args.local_dir)) if args.dry_run else s3
    return Context(wave, stack, clients, store, args.dry_run)


# ---------------------------------------------------------------- e5_submit


def build_submit_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="e5_submit.py", description="the E5 wave's rolling submitter")
    _common(parser)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser(
        "run", help="pre-flight, then top up every startable arm each cycle until the wave settles"
    )
    run.add_argument("--once", action="store_true", help="one cycle, then exit")
    run.add_argument("--max-cycles", type=int)
    sub.add_parser("status", help="the heartbeat and the ledger's state (no outcome columns)")
    sub.add_parser("preflight", help="S-0 for the wave and each arm whose sweep exists")
    gate = sub.add_parser(
        "gate", help="the S-1 gate's state; --rerun asks the submitter to evaluate it again"
    )
    gate.add_argument("--rerun", action="store_true")
    ping = sub.add_parser("ping", help="keep one import warm while the submitter's heartbeat asks for it")
    ping.add_argument("--import", dest="import_name", required=True)
    ping.add_argument("--interval", type=float, default=30.0)
    ping.add_argument("--restart", action="store_true", help="forget a failed warm-up (S-5: retry later)")
    pause = sub.add_parser("pause", help="pause an arm at the next cycle")
    pause.add_argument("arm")
    pause.add_argument("--reason", default="")
    resume = sub.add_parser("resume", help="resume a paused arm; its rules restart from now")
    resume.add_argument("arm")
    sub.add_parser("clear-stop", help="clear a hard stop (after Mahmood's decision)")
    window = sub.add_parser("window", help="open or close an arm's metering window (probes, labeller)")
    window.add_argument("arm")
    window.add_argument("edge", choices=["start", "end"])
    window.add_argument("--usd", type=float, help="spend outside AWS, recorded when the window ends")
    unkill = sub.add_parser("unkill", help="let the pinger warm an import again after S-6 stopped it")
    unkill.add_argument("import_name")
    sub.add_parser("month-fit", help="section 10.4's sum for each month-fit arm (read-only)")
    return parser


def _cmd_run(ctx: Context, args: argparse.Namespace, sleep: Callable[[float], None]) -> int:
    submitter = ctx.submitter(args.root)
    now = submitter.clock()
    failures = submitter.preflight(now, include_stop=False)
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    submitter.adopt_unrecorded_jobs()
    cycles = 0
    while True:
        submitter.cycle()
        cycles += 1
        if args.once or submitter.finished() or (args.max_cycles and cycles >= args.max_cycles):
            break
        sleep(ctx.wave.poll_s)
    if submitter.finished():
        print("every arm has settled")
    return 0


def _cmd_status(ctx: Context, args: argparse.Namespace) -> int:
    ledger = ctx.ledger()
    state = ledger.state()
    heartbeat = ledger.read_heartbeat()
    shown = {
        "heartbeat": heartbeat,
        "hard_stop": state.hard_stop,
        "paused": state.paused,
        "gate": state.gate,
        "cap_cut": state.cap_cut,
        "ramp": state.ramp,
        "month_fit": state.month_fit,
        "windows": state.windows,
        "kill_pings": state.kill_pings,
        "deleted_imports": state.deleted_imports,
        "warm": {n: ledger.warm(n) for n in sorted({a.import_name for a in ctx.wave.arms if a.import_name})},
    }
    print(mask(json.dumps(shown, indent=1, sort_keys=True, default=str)))
    return 0


def _cmd_preflight(ctx: Context, args: argparse.Namespace) -> int:
    submitter = ctx.submitter(args.root)
    failures = submitter.preflight(submitter.clock())
    for arm in submitter.batch_arms():
        if not (args.root / str(arm.sweep)).exists():
            failures.append(f"S-0: {arm.name}: {arm.sweep} does not exist")
            continue
        run = submitter.arm_run(arm)
        failures += [f"{arm.name}: {f}" for f in submitter.arm_preflight(run) if f not in failures]
    print("\n".join(failures) if failures else "S-0: pre-flight passed")
    return 1 if failures else 0


def _cmd_month_fit(ctx: Context, args: argparse.Namespace) -> int:
    submitter = ctx.submitter(args.root)
    submitter.observe()
    now = submitter.clock()
    for arm in ctx.wave.arms:
        if not arm.month_fit:
            continue
        if arm.canary_usd_per_episode is None:
            print(f"{arm.name}: no canary cost per episode in the config")
            continue
        result = submitter.month_fit(arm, now)
        print(json.dumps({"arm": arm.name, **result.as_dict()}, sort_keys=True))
    return 0


def _cmd_ping(ctx: Context, args: argparse.Namespace) -> int:
    name = args.import_name

    def arn() -> str | None:
        try:
            return str(ctx.clients("bedrock").get_imported_model(modelIdentifier=name)["modelArn"])
        except Exception:
            return None

    pinger = Pinger(
        ctx.ledger(),
        name,
        arn,
        ctx.wave.rules,
        invoke=None if ctx.dry_run else lambda a: invoke_ping(ctx.clients("bedrock-runtime"), a),
        dry_run=ctx.dry_run,
        interval_s=args.interval,
    )
    if args.restart:
        pinger.restart()
    reason = pinger.run()
    print(f"pinger for {name} exited: {reason}")
    return 0 if reason == "done" else 1


def _command(ctx: Context, args: argparse.Namespace) -> int:
    ledger, now = ctx.ledger(), utcnow()
    if args.command in ("pause", "resume", "window"):
        ctx.wave.arm(args.arm)
    if args.command == "pause":
        ledger.command(now, "pause", arm=args.arm, reason=args.reason)
    elif args.command == "resume":
        ledger.command(now, "resume", arm=args.arm)
    elif args.command == "clear-stop":
        ledger.command(now, "clear-stop")
    elif args.command == "window":
        ledger.command(now, "window", arm=args.arm, edge=args.edge, at=iso(now), usd=args.usd)
    elif args.command == "unkill":
        ledger.command(now, "unkill", **{"import": args.import_name})
    elif args.command == "gate":
        if args.rerun:
            ledger.command(now, "gate")
        print(json.dumps(ledger.state().gate, indent=1, default=str))
        return 0
    where = ctx.store.where(ledger.key("commands.log"))
    print(f"{args.command}: queued for the submitter's next cycle ({where})")
    return 0


def submit_main(
    argv: Sequence[str] | None = None,
    factory: Factory | None = None,
    env: dict[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = build_submit_parser().parse_args(argv)
    ctx = context(args, factory, env)
    try:
        if args.command == "run":
            return _cmd_run(ctx, args, sleep)
        if args.command == "status":
            return _cmd_status(ctx, args)
        if args.command == "preflight":
            return _cmd_preflight(ctx, args)
        if args.command == "month-fit":
            return _cmd_month_fit(ctx, args)
        if args.command == "ping":
            return _cmd_ping(ctx, args)
        return _command(ctx, args)
    except PreflightFailed as exc:
        print(f"S-0: {exc}", file=sys.stderr)
        return 1
    except LookupError as exc:
        print(exc, file=sys.stderr)
        return 2


# ---------------------------------------------------------------- e5_meter


def build_meter_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="e5_meter.py", description="the wave's real-time cost meter")
    _common(parser)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("now", help="this month's metered spend and estimate, and each arm's spend so far")
    validate = sub.add_parser(
        "validate", help="a finished sweep's traced tokens and Batch run time against CloudWatch's"
    )
    validate.add_argument(
        "--sweep", default="e4-claude", help="the sweep name under runs/ (and its job names)"
    )
    validate.add_argument(
        "--model", default="us.anthropic.claude-sonnet-4-6", help="its system inference profile"
    )
    validate.add_argument("--runs-dir", type=Path, help="a local mirror of runs/<sweep>/ (default: read S3)")
    validate.add_argument(
        "--margin-min", type=float, default=10.0, help="minutes added around the jobs' span"
    )
    return parser


def _cmd_meter_now(ctx: Context, args: argparse.Namespace) -> int:
    submitter = ctx.submitter(args.root)
    now = submitter.clock()
    submitter.observe()
    usage = submitter.month_meter(now)
    arms = {}
    for arm in ctx.wave.arms:
        if submitter.state.windows.get(arm.name):
            arms[arm.name] = round(submitter.arm_spent(arm, now), 2)
    month = meter.month_key(now)
    out = {
        "at": iso(now),
        "month": month,
        "metered": usage.as_dict(),
        "estimate_usd": round(submitter.month_estimate(now), 2),
        "stop_usd": ctx.wave.months[month].stop_usd if month in ctx.wave.months else None,
        "arms_usd": arms,
    }
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0


def validate(
    ctx: Context, sweep: str, model: str, runs: Runs, margin: timedelta, now: datetime
) -> tuple[list[meter.Agreement], dict[str, Any]]:
    arm = next((a for a in ctx.wave.arms if a.bedrock_model == model and a.price), None)
    if arm is None or arm.price is None:
        raise LookupError(f"no arm prices {model}")
    profile = ctx.stack.profile_id(model)
    if profile is None:
        raise LookupError(f"the stack has no tagged profile for {model}")
    spans = meter.batch_attempts(ctx.clients("batch"), ctx.stack.queue, f"{sweep}-")
    if not spans:
        raise LookupError(f"no finished attempts of jobs named {sweep}-*")
    start = datetime.fromtimestamp(min(s for s, _ in spans), now.tzinfo) - margin
    end = datetime.fromtimestamp(max(e for _, e in spans), now.tzinfo) + margin
    tokens = meter.traced_tokens(runs.read(folder, "episode.json") for folder in runs.folders())
    sources = meter.Sources(tokens=(meter.Tokens(profile, arm.price),), queue=ctx.stack.queue)
    cloudwatch = meter.cloudwatch_usage(ctx.clients("cloudwatch"), ctx.wave, sources, start, end, now)
    seconds = sum(e - s for s, e in spans)
    found = meter.agreements(ctx.wave, arm.price, tokens, seconds, cloudwatch)
    window = {
        "start": iso(start),
        "end": iso(end),
        "attempts": len(spans),
        "episode_files": len(runs.folders()),
    }
    return found, window


def _cmd_meter_validate(ctx: Context, args: argparse.Namespace) -> int:
    runs: Runs = (
        LocalRuns(args.runs_dir)
        if args.runs_dir
        else S3Runs(ctx.clients("s3"), ctx.stack.bucket, f"{ctx.wave.runs_prefix}/{args.sweep}")
    )
    found, window = validate(ctx, args.sweep, args.model, runs, timedelta(minutes=args.margin_min), utcnow())
    print(json.dumps({"window": window, "agreement": [a.as_dict() for a in found]}, indent=1))
    worst = max(abs(a.difference) for a in found)
    print(f"largest difference {100 * worst:.2f}% ({'within' if worst <= 0.05 else 'OUTSIDE'} 5%)")
    return 0 if worst <= 0.05 else 1


def meter_main(
    argv: Sequence[str] | None = None, factory: Factory | None = None, env: dict[str, str] | None = None
) -> int:
    args = build_meter_parser().parse_args(argv)
    ctx = context(args, factory, env)
    if args.command == "now":
        return _cmd_meter_now(ctx, args)
    return _cmd_meter_validate(ctx, args)


# ---------------------------------------------------------------- lip e5 health


def add_e5_parser(sub: Any) -> None:
    e5 = sub.add_parser("e5", help="the E5 wave (health; the submitter and meter are scripts/e5_*.py)")
    e5_sub = e5.add_subparsers(dest="e5_command", required=True)
    h = e5_sub.add_parser("health", help="blinded health per arm and cell; every call is logged")
    _common(h)
    h.add_argument("--arm", action="append", help="only these arms (repeatable)")
    h.add_argument("--runs", type=Path, help="offline: a local mirror with runs/<sweep name>/ (no AWS)")
    h.add_argument("--no-cost", action="store_true", help="skip the metered cost and throttles")
    h.add_argument("--json", action="store_true")
    h.set_defaults(func=lambda a: health_cmd(a))


def health_cmd(
    args: argparse.Namespace,
    factory: Factory | None = None,
    env: dict[str, str] | None = None,
    out: Callable[[str], None] = print,
) -> int:
    query = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items() if k != "func"}
    wave = config.load(args.config)
    if args.runs:
        ctx = Context(
            wave, Stack(wave.stack, "", "", "", ""), Clients(_offline), LocalStore(args.local_dir), True
        )
        runs_root: Path = args.runs

        def runs_factory(prefix: str) -> Runs:
            return LocalRuns(runs_root / prefix.removeprefix(f"{wave.runs_prefix}/"))

        log_store: Store = ctx.store
        who = "local"
        submitter = ctx.submitter(args.root, runs=runs_factory)
    else:
        ctx = context(args, factory, env)
        log_store = LocalStore(args.local_dir) if args.dry_run else ctx.store
        who = health.principal(ctx.clients("sts"))
        submitter = ctx.submitter(args.root)
    arms = [a for a in wave.arms if a.kind == "batch" and (not args.arm or a.name in args.arm)]
    if args.arm:
        for name in args.arm:
            wave.arm(name)
    selected = []
    for arm in arms:
        if not (args.root / str(arm.sweep)).exists():
            continue
        run = submitter.arm_run(arm)
        if not args.runs:
            submitter.refresh_jobs(run)
        selected.append(run)
    submitter.scan(selected, log=False)
    costs: dict[str, health.ArmCost] = {}
    if not args.runs and not args.no_cost:
        now = submitter.clock()
        for run in selected:
            spent = submitter.arm_spent(run.arm, now) if submitter.state.windows.get(run.arm.name) else 0.0
            costs[run.arm.name] = health.ArmCost(spent, _throttles(submitter, run.arm.name, now))
    health.report(
        [(r.arm.name, r.episodes.values(), r.live) for r in selected],
        costs,
        log=lambda rows: health.log_query(log_store, wave.queries_log, utcnow(), who, query, rows),
        out=out,
        json_out=args.json,
    )
    return 0


def _throttles(submitter: Submitter, arm_name: str, now: datetime) -> float | None:
    arm = submitter.wave.arm(arm_name)
    model_id = (
        submitter.stack.profile_id(arm.bedrock_model)
        if arm.bedrock_model
        else submitter.import_arn(arm.import_name)
        if arm.import_name
        else None
    )
    if model_id is None:
        return None
    total = 0.0
    for start, end in submitter.windows(arm_name, now):
        query = meter.metric_query("t", "AWS/Bedrock", "InvocationThrottles", "ModelId", model_id, 300)
        total += meter.metric_sums(submitter.clients("cloudwatch"), [query], start, end or now)["t"]
    return total


def _offline(service: str) -> Any:
    raise RuntimeError(f"offline health (--runs) uses no AWS, but asked for {service}")
