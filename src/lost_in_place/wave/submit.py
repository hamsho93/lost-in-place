"""The rolling per-arm submitter (plan section 10.5, operations; `docs/runbooks/e5-wave.md`).

Each cycle it reads the ledger and the results, classifies new attempts by the S4 rule, applies the
stop rules, and tops each startable arm up to its in-flight cap with small arrays of the arm's open
manifest entries, in manifest order. An entry is open while it has no result, fewer than three
attempts and no live job; so a restart resubmits nothing that has a result or is still running.
No whole-arm arrays are ever submitted.

In a dry run all AWS clients are read-only and the ledger is written to a local directory.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from lost_in_place import manifest
from lost_in_place.llm.registry import ModelSpec
from lost_in_place.manifest import Entry
from lost_in_place.wave import meter, rules
from lost_in_place.wave.aws import Clients, Stack, account_id, mask, utcnow
from lost_in_place.wave.config import After, Arm, Wave
from lost_in_place.wave.episodes import Episode, Runs, S3Runs, Scanner, attempts_in_order, results_in_order
from lost_in_place.wave.ledger import JobRecord, Ledger, iso, parse_iso
from lost_in_place.wave.store import Store

TERMINAL = ("SUCCEEDED", "FAILED")
JOB_NAME = re.compile(r"[^A-Za-z0-9_-]")
DEFAULT_MODEL_REGION = str(ModelSpec.model_fields["region"].default)


class PreflightFailed(RuntimeError):
    pass


def contiguous(indices: Sequence[int], max_size: int) -> list[tuple[int, int]]:
    """(offset, size) runs of consecutive indices, each at most `max_size` long."""
    runs: list[tuple[int, int]] = []
    for i in sorted(indices):
        if runs and runs[-1][0] + runs[-1][1] == i and runs[-1][1] < max_size:
            runs[-1] = (runs[-1][0], runs[-1][1] + 1)
        else:
            runs.append((i, 1))
    return runs


@dataclass
class Sweep:
    name: str
    manifest_key: str
    body: bytes
    entries: list[Entry]
    scanner: Scanner


@dataclass
class ArmRun:
    arm: Arm
    sweep: Sweep
    indices: list[int]
    jobs: list[JobRecord] = field(default_factory=list)
    child_status: dict[str, str] = field(default_factory=dict)
    child_started: dict[str, float] = field(default_factory=dict)
    episodes: dict[int, Episode] = field(default_factory=dict)
    live: set[int] = field(default_factory=set)
    running: int = 0
    queued: int = 0
    submissions: Counter[int] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return len(self.indices)

    @property
    def settled(self) -> int:
        return sum(e.settled for e in self.episodes.values())

    @property
    def done(self) -> bool:
        return self.total > 0 and self.settled == self.total and not self.live

    @property
    def started(self) -> bool:
        return bool(self.jobs)

    def open_indices(self, max_submissions: int) -> list[int]:
        return [
            i
            for i in self.indices
            if not self.episodes[i].settled
            and i not in self.live
            and not self.episodes[i].pending
            and self.submissions[i] < max_submissions
        ]


class Submitter:
    def __init__(
        self,
        wave: Wave,
        clients: Clients,
        stack: Stack,
        store: Store,
        *,
        root: Path = Path(),
        dry_run: bool = False,
        clock: Callable[[], datetime] = utcnow,
        runs: Callable[[str], Runs] | None = None,
        run_command: Callable[[Sequence[str]], tuple[int, str]] | None = None,
        models_path: Path | None = None,
        out: Callable[[str], None] = print,
    ) -> None:
        self.wave = wave
        self.clients = clients
        self.stack = stack
        self.store = store
        self.root = root
        self.dry_run = dry_run
        self.clock = clock
        self.runs_factory = runs or (lambda prefix: S3Runs(clients("s3"), stack.bucket, prefix))
        self.run_command = run_command or _run_command
        self.models_path = models_path or root / "src/lost_in_place/llm/models.yaml"
        self.out = out
        self.ledger = Ledger(store, wave.ledger_prefix)
        self.state = self.ledger.state()
        self.sweeps: dict[str, Sweep] = {}
        self.arms: dict[str, ArmRun] = {}
        self.s4_seen = self.ledger.s4_logged()
        self.idle_since: dict[str, datetime] = {}
        self._account: str | None = None
        self._imports_cache: tuple[datetime, list[str]] | None = None
        self._dry_jobs = 0
        self._estimate: tuple[datetime, float] | None = None
        self._registry: dict[str, Any] | None = None

    # ---------------------------------------------------------------- loading

    def job_prefix(self, arm: Arm) -> str:
        """Batch job names allow letters, digits, '-' and '_' only."""
        return JOB_NAME.sub("-", f"{self.wave.name}-{arm.name}-")[:100]

    def batch_arms(self) -> list[Arm]:
        return [a for a in self.wave.arms if a.kind == "batch"]

    def sweep(self, path: str) -> Sweep:
        if path not in self.sweeps:
            spec_path = self.root / path
            if not spec_path.exists():
                raise PreflightFailed(f"sweep {path} does not exist (written by scripts/e5_sweeps.py)")
            spec = manifest.SweepSpec.load(spec_path)
            entries = manifest.build(spec, self.root)
            body = manifest.dumps(spec, entries).encode()
            scanner = Scanner(
                self.runs_factory(f"{self.wave.runs_prefix}/{spec.name}"),
                dict(enumerate(entries)),
                clock=lambda: self.clock().timestamp(),
            )
            key = f"{self.wave.manifests_prefix}/{spec.name}.json"
            self.sweeps[path] = Sweep(spec.name, key, body, entries, scanner)
        return self.sweeps[path]

    def arm_run(self, arm: Arm) -> ArmRun:
        if arm.name not in self.arms:
            assert arm.sweep is not None
            sweep = self.sweep(arm.sweep)
            indices = [
                i
                for i, e in enumerate(sweep.entries)
                if arm.select is None or arm.select.matches(e.scenario_id, e.observation)
            ]
            run = ArmRun(arm, sweep, indices, jobs=self.ledger.jobs(arm.name))
            self.arms[arm.name] = run
        return self.arms[arm.name]

    def adopt_unrecorded_jobs(self) -> None:
        """Jobs submitted but never recorded (the submitter died between the two) join the ledger."""
        batch = self.clients("batch")
        for arm in self.batch_arms():
            run = self.arm_run(arm)
            known = {j.job_id for j in run.jobs}
            prefix = self.job_prefix(arm)
            pages = batch.get_paginator("list_jobs").paginate(
                jobQueue=self.stack.queue, filters=[{"name": "JOB_NAME", "values": [f"{prefix}*"]}]
            )
            unknown = [
                j["jobId"]
                for page in pages
                for j in page.get("jobSummaryList", [])
                if meter.job_of(j["jobName"], prefix)
                and j["jobId"] not in known
                and j.get("status") not in TERMINAL
            ]
            for i in range(0, len(unknown), 100):
                for job in batch.describe_jobs(jobs=unknown[i : i + 100])["jobs"]:
                    params = job.get("parameters", {})
                    if not str(params.get("manifest", "")).endswith(run.sweep.manifest_key):
                        continue
                    size = int((job.get("arrayProperties") or {}).get("size", 1))
                    record = JobRecord(
                        arm=arm.name,
                        job_id=job["jobId"],
                        job_name=job["jobName"],
                        manifest=run.sweep.manifest_key,
                        offset=int(params.get("offset", 0)),
                        size=size,
                        submitted_at=iso(
                            datetime.fromtimestamp(job["createdAt"] / 1000, self.clock().tzinfo)
                        ),
                    )
                    self.ledger.record_job(record)
                    run.jobs.append(record)
                    self.ledger.event(self.clock(), "adopted", arm=arm.name, job=record.job_id)

    # ---------------------------------------------------------------- reading

    def refresh_jobs(self, run: ArmRun) -> None:
        open_ids = [
            cid
            for job in run.jobs
            if not job.dry_run
            for cid in job.child_ids()
            if run.child_status.get(cid) not in TERMINAL
        ]
        batch = self.clients("batch")
        for i in range(0, len(open_ids), 100):
            for job in batch.describe_jobs(jobs=open_ids[i : i + 100])["jobs"]:
                cid, status = job["jobId"], job["status"]
                run.child_status[cid] = status
                if status in TERMINAL:
                    seconds = sum(
                        (a["stoppedAt"] - a["startedAt"]) / 1000
                        for a in job.get("attempts", [])
                        if a.get("startedAt") and a.get("stoppedAt")
                    )
                    self.state.compute_s[cid] = round(seconds, 3)
                    run.child_started.pop(cid, None)
                elif status == "RUNNING" and job.get("startedAt"):
                    run.child_started[cid] = job["startedAt"] / 1000
        run.live, run.running, run.queued = set(), 0, 0
        for job in run.jobs:
            for cid in job.child_ids():
                status = "SUBMITTED" if job.dry_run else run.child_status.get(cid, "SUBMITTED")
                if status in TERMINAL:
                    continue
                run.live.add(job.index_of_child(cid))
                if status == "RUNNING":
                    run.running += 1
                else:
                    run.queued += 1
        run.submissions = Counter(i for job in run.jobs for i in job.indices)

    def observe(self) -> list[ArmRun]:
        """Every Batch arm's jobs and episodes, without writing anything (status, health, month fit)."""
        runs = [self.arm_run(a) for a in self.batch_arms() if (self.root / str(a.sweep)).exists()]
        for run in runs:
            self.refresh_jobs(run)
        self.scan(runs, log=False)
        return runs

    def scan(self, runs: list[ArmRun], *, log: bool = True) -> None:
        by_sweep: dict[str, list[ArmRun]] = {}
        for run in runs:
            by_sweep.setdefault(run.sweep.name, []).append(run)
        now = self.clock()
        lines: list[tuple[str, str, str, str, str]] = []
        for group in by_sweep.values():
            episodes, new = group[0].sweep.scanner.scan()
            owner = {i: run.arm.name for run in group for i in run.indices}
            for run in group:
                run.episodes = {i: episodes[i] for i in run.indices}
            for view in new:
                index = group[0].sweep.scanner.by_key[view.key]
                if log and index in owner and (view.key, view.label) not in self.s4_seen:
                    verdict = "result" if view.counts else "retry"
                    lines.append((iso(now), owner[index], view.key, view.label, verdict))
                    self.s4_seen.add((view.key, view.label))
        self.ledger.s4(lines)

    # ---------------------------------------------------------------- regions

    def registry(self) -> dict[str, Any]:
        """The raw models.yaml (S-0 compares entries field by field, so no defaults are filled in)."""
        if self._registry is None:
            path = self.models_path
            self._registry = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
        return self._registry

    def region_of(self, arm: Arm) -> str | None:
        """Where the arm's Bedrock calls are served, so where CloudWatch has its metrics: the region of
        its models.yaml entry, else of its tagged profile's ARN (None: the home region)."""
        entry = self.registry().get(arm.model) if arm.model else None
        if isinstance(entry, dict):
            return str(entry.get("region") or DEFAULT_MODEL_REGION)
        return self.stack.profile_region(arm.bedrock_model) if arm.bedrock_model else None

    def profile_regions(self) -> dict[str, str | None]:
        regions = {model: self.stack.profile_region(model) for model in self.stack.bedrock_profiles}
        for arm in self.wave.arms:
            if arm.bedrock_model and arm.model:
                regions[arm.bedrock_model] = self.region_of(arm)
        return regions

    def cw(self, region: str | None = None) -> Any:
        return self.clients("cloudwatch", region)

    def model_source(self, arm: Arm) -> tuple[str | None, str | None]:
        """The arm's CloudWatch `ModelId` and the region its metrics are in."""
        if arm.bedrock_model:
            return self.stack.profile_id(arm.bedrock_model), self.region_of(arm)
        if arm.import_name:
            return self.import_arn(arm.import_name), None
        return None, None

    def arm_throttles(self, arm: Arm, now: datetime) -> float | None:
        """InvocationThrottles over the arm's metering windows."""
        model_id, region = self.model_source(arm)
        if model_id is None:
            return None
        total = 0.0
        for start, end in self.windows(arm.name, now):
            query = meter.metric_query("t", "AWS/Bedrock", "InvocationThrottles", "ModelId", model_id, 300)
            total += meter.metric_sums(self.cw(region), [query], start, end or now)["t"]
        return total

    # ---------------------------------------------------------------- money

    def account(self) -> str:
        if self._account is None:
            self._account = account_id(self.clients("sts"))
        return self._account

    def import_arn(self, name: str) -> str | None:
        if name in self.state.import_arns:
            return self.state.import_arns[name]
        warm = self.ledger.warm(name)
        if warm and warm.get("arn"):
            self.state.import_arns[name] = str(warm["arn"])
            return self.state.import_arns[name]
        try:
            arn = str(self.clients("bedrock").get_imported_model(modelIdentifier=name)["modelArn"])
        except Exception:
            return None
        self.state.import_arns[name] = arn
        return arn

    def project_imports(self, now: datetime) -> list[str]:
        if self._imports_cache is None or now - self._imports_cache[0] > timedelta(minutes=10):
            known = set(self.state.import_arns.values())
            self._imports_cache = (
                now,
                sorted(known | set(meter.all_import_arns(self.cw()))),
            )
        return self._imports_cache[1]

    def in_flight_compute(self, now: datetime, runs: list[ArmRun] | None = None) -> float:
        seconds = sum(
            max(0.0, now.timestamp() - started)
            for run in (runs if runs is not None else self.arms.values())
            for started in run.child_started.values()
        )
        return meter.compute_usd(self.wave, seconds)

    def external_usd(self, month: str) -> float:
        return sum(
            float(w.get("usd") or 0)
            for windows in self.state.windows.values()
            for w in windows
            if w.get("end") and str(w["end"]).startswith(month)
        )

    def project_sources(self, now: datetime) -> meter.Sources:
        return meter.project_sources(
            self.wave,
            self.stack.bedrock_profiles,
            self.project_imports(now),
            self.stack.queue,
            self.profile_regions(),
        )

    def month_meter(self, now: datetime) -> meter.Usage:
        """Metered AWS spend this month: CloudWatch, plus attempts still running, plus other tagged spend."""
        sources = self.project_sources(now)
        start = meter.month_start(now)
        usage = meter.cloudwatch_usage(self.cw, self.wave, sources, start, now, now)
        usage.compute_usd += self.in_flight_compute(now)
        usage.other_usd = self.wave.other_usd_per_day * (now - start).total_seconds() / 86400
        return usage

    def month_estimate(self, now: datetime) -> float:
        if self._estimate is not None and self._estimate[0] == now:
            return self._estimate[1]
        self._estimate = (now, self._month_estimate(now))
        return self._estimate[1]

    def _month_estimate(self, now: datetime) -> float:
        month = meter.month_key(now)
        rule = self.wave.months.get(month)
        if rule is not None and rule.budget_actual:
            actual, updated = meter.budget_actual(
                self.clients("budgets"), self.account(), self.stack.budget_name
            )
            since = max(meter.month_start(now), updated - timedelta(hours=self.wave.budget_lag_h))
            recent = meter.cloudwatch_usage(self.cw, self.wave, self.project_sources(now), since, now, now)
            return actual + recent.total_usd + self.in_flight_compute(now) + self.external_usd(month)
        return self.month_meter(now).total_usd + self.external_usd(month)

    def windows(self, arm: str, now: datetime) -> list[tuple[datetime, datetime | None]]:
        return [
            (parse_iso(w["start"]), parse_iso(w["end"]) if w.get("end") else None)
            for w in self.state.windows.get(arm, [])
        ]

    def arm_spent(self, arm: Arm, now: datetime) -> float:
        sources = meter.arm_sources(
            arm,
            self.stack.bedrock_profiles,
            {arm.import_name: arn} if arm.import_name and (arn := self.import_arn(arm.import_name)) else {},
            self.region_of(arm),
        )
        usage = meter.window_usage(self.cw, self.wave, sources, self.windows(arm.name, now), now)
        spent = usage.total_usd + sum(float(w.get("usd") or 0) for w in self.state.windows.get(arm.name, []))
        if arm.kind == "batch":
            run = self.arm_run(arm)
            seconds = sum(self.state.compute_s.get(cid, 0.0) for job in run.jobs for cid in job.child_ids())
            spent += meter.compute_usd(self.wave, seconds) + self.in_flight_compute(now, [run])
        return spent

    def arm_done(self, name: str) -> bool:
        arm = self.wave.arm(name)
        if arm.kind == "external":
            windows = self.state.windows.get(name, [])
            return bool(windows) and all(w.get("end") for w in windows)
        return self.arm_run(arm).done

    def settled_fraction(self, cond: After) -> float:
        arm = self.wave.arm(cond.arm)
        if arm.kind == "external":
            return 1.0 if self.arm_done(arm.name) else 0.0
        run = self.arm_run(arm)
        return run.settled / run.total if run.total else 0.0

    def remaining_usd(self, arm: Arm, now: datetime) -> float:
        if self.arm_done(arm.name):
            return 0.0
        if arm.kind == "batch":
            run = self.arm_run(arm)
            if run.settled >= 10:
                spent = self.arm_spent(arm, now)
                return max(0.0, spent / run.settled * run.total - spent)
        if not self.state.windows.get(arm.name):
            return arm.estimate_usd
        return max(0.0, arm.estimate_usd - self.arm_spent(arm, now))

    def month_fit(self, arm: Arm, now: datetime) -> rules.MonthFitResult:
        assert arm.canary_usd_per_episode is not None
        month = meter.month_key(now)
        nxt = meter.month_key(meter.month_start(now) + timedelta(days=32))
        next_rule = self.wave.months.get(nxt)
        next_budget = (
            next_rule.stop_usd
            if next_rule and (next_rule.arms is None or arm.name in next_rule.arms)
            else None
        )
        deadline = self.wave.deferral_deadline
        others = [a for a in self.wave.outside_month_fit() if a.name != arm.name]
        remaining = sum(self.remaining_usd(a, now) for a in others) + self.wave.fixed_remaining_usd
        run = self.arm_run(arm)
        episodes = run.total - run.settled
        return rules.month_fit(
            self.month_estimate(now),
            remaining,
            episodes,
            arm.canary_usd_per_episode,
            arm.probe_usd,
            self.wave.rules,
            self.wave.months[month].stop_usd,
            next_month_budget=next_budget,
            deadline_passed=deadline is not None and now.date() > date.fromisoformat(deadline),
        )

    # ---------------------------------------------------------------- rules

    def pause(self, arm: str, reason: str, now: datetime) -> None:
        if arm not in self.state.paused:
            self.state.paused[arm] = reason
            self.ledger.event(now, "pause", arm=arm, reason=reason)
            self.out(f"PAUSE {arm}: {reason}")

    def hard_stop(self, reason: str, now: datetime) -> None:
        if not self.state.hard_stop:
            self.state.hard_stop = reason
            self.ledger.event(now, "hard_stop", reason=reason)
            self.out(f"HARD STOP: {reason}")

    def apply_commands(self, now: datetime) -> None:
        commands = self.ledger.commands()
        for cmd in commands[self.state.commands_applied :]:
            name, arm = cmd["command"], cmd.get("arm")
            if name == "pause" and arm:
                self.pause(arm, f"operator: {cmd.get('reason', '')}", now)
            elif name == "resume" and arm:
                self.state.paused.pop(arm, None)
                run = self.arms.get(arm)
                if run is not None:
                    self.state.acked[arm] = {
                        "S-2": len(attempts_in_order(run.episodes.values())),
                        "S-3": 1,
                        "S-8": len(results_in_order(run.episodes.values())),
                    }
                if self.wave.arm(arm).month_fit:
                    self.state.month_fit = None
            elif name == "clear-stop":
                self.state.hard_stop = None
            elif name == "window" and arm:
                windows = self.state.windows.setdefault(arm, [])
                if cmd["edge"] == "start":
                    windows.append({"start": cmd["at"], "end": None})
                elif windows and not windows[-1].get("end"):
                    windows[-1]["end"] = cmd["at"]
                    if cmd.get("usd") is not None:
                        windows[-1]["usd"] = float(cmd["usd"])
            elif name == "unkill" and cmd.get("import"):
                self.state.kill_pings.pop(str(cmd["import"]), None)
            elif name == "gate":
                self.state.gate = None
            self.ledger.event(now, "applied", command=cmd)
        self.state.commands_applied = len(commands)

    def check_arm(self, run: ArmRun, now: datetime) -> None:
        """S-2, S-3, S-4, S-7 and S-8 on a started arm."""
        name, acked = run.arm.name, self.state.acked.get(run.arm.name, {})
        attempts = attempts_in_order(run.episodes.values())
        results = results_in_order(run.episodes.values())
        reason = rules.s2_harness(attempts[acked.get("S-2", 0) :], self.wave.rules)
        if reason is None and "S-3" not in acked:
            reason = rules.s3_invalid(results, self.wave.rules)
        if reason:
            self.pause(name, reason, now)
        self.check_throttles(run, now)
        settled = run.settled
        if rules.s7_due(settled, self.state.s7_checked.get(name, 0), self.wave.rules) and not run.done:
            self.state.s7_checked[name] = settled
            self.check_s7(run, now)
        if run.arm.month_fit and run.arm.price and run.arm.canary_usd_per_episode:
            costs = [
                run.arm.price.cost(r.input_tokens, r.output_tokens) for r in results[acked.get("S-8", 0) :]
            ]
            reason = rules.s8_rolling(costs, run.arm.canary_usd_per_episode, self.wave.rules)
            if reason:
                self.pause(name, reason, now)

    def check_throttles(self, run: ArmRun, now: datetime) -> None:
        arm = run.arm
        model_id, region = self.model_source(arm)
        if not model_id or run.done:
            return
        last = self.state.last_cap_cut.get(arm.name)
        window = timedelta(seconds=self.wave.rules.throttle_window_s)
        if last and now - parse_iso(last) < window:
            return
        queries = [
            meter.metric_query("t", "AWS/Bedrock", "InvocationThrottles", "ModelId", model_id, 60),
            meter.metric_query("n", "AWS/Bedrock", "Invocations", "ModelId", model_id, 60),
        ]
        sums = meter.metric_sums(self.cw(region), queries, now - window, now)
        if rules.s4_throttled(sums["t"], sums["n"], self.wave.rules):
            self.state.cap_cut[arm.name] = (
                self.state.cap_cut.get(arm.name, 0) + self.wave.rules.throttle_cap_cut
            )
            self.state.last_cap_cut[arm.name] = iso(now)
            self.ledger.event(now, "cap_cut", arm=arm.name, cut=self.state.cap_cut[arm.name], rule="S-4")
            self.out(f"S-4: {arm.name} throttled; cap cut by {self.wave.rules.throttle_cap_cut}")

    def check_s7(self, run: ArmRun, now: datetime) -> None:
        arm = run.arm
        spent = self.arm_spent(arm, now)
        month = meter.month_key(now)
        stop = self.wave.months[month].stop_usd if month in self.wave.months else 0.0
        estimate = self.month_estimate(now)
        others = sum(self.remaining_usd(a, now) for a in self.wave.outside_month_fit() if a.name != arm.name)
        projected = spent / run.settled * run.total if run.settled else spent
        base = estimate + max(0.0, projected - spent) + others + self.wave.fixed_remaining_usd
        gpt = [a for a in self.wave.arms if a.month_fit and not self.arm_done(a.name)]
        gpt_usd = sum(
            self.wave.rules.month_fit_factor
            * (self.arm_run(a).total * (a.canary_usd_per_episode or 0) + a.probe_usd)
            for a in gpt
            if a.kind == "batch" and a.sweep and (self.root / a.sweep).exists()
        )
        verdict = rules.s7_projection(
            arm.name, spent, run.settled, run.total, self.wave.cap_usd(arm), base + gpt_usd, base, stop
        )
        self.ledger.event(
            now, "s7", arm=arm.name, spent=round(spent, 2), projected=round(verdict.projected_usd, 2)
        )
        if verdict.verdict == "pause":
            self.pause(arm.name, verdict.reason, now)
        elif verdict.verdict == "continue-gpt-december":
            self.state.month_fit = {"decision": "defer", "reason": verdict.reason, "at": iso(now)}
            self.ledger.event(now, "month_fit", decision="defer", reason=verdict.reason)
        elif verdict.reason:
            self.ledger.event(now, "s7_over_cap", arm=arm.name, reason=verdict.reason)

    def preflight(self, now: datetime, *, include_stop: bool = True) -> list[str]:
        """S-0 for the whole wave: the budget action is armed and hasn't fired, the meter reads, and no
        unexpected import exists."""
        failures = []
        budgets = self.clients("budgets")
        try:
            actions = budgets.describe_budget_actions_for_budget(
                AccountId=self.account(), BudgetName=self.stack.budget_name
            )["Actions"]
            if not actions:
                failures.append("S-0: the budget has no stop action")
            for action in actions:
                if action.get("Status") != "STANDBY":
                    failures.append(f"S-0: budget action is {action.get('Status')}, not STANDBY")
        except Exception as exc:
            failures.append(f"S-0: cannot read the budget actions: {_brief(exc)}")
        iam = self.clients("iam")
        for role in (self.stack.operator_role_name, self.stack.job_role_name):
            try:
                attached = iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]
            except Exception as exc:
                failures.append(f"S-0: cannot check BudgetStop on {role}: {_brief(exc)}")
                continue
            if any(p["PolicyArn"] == self.stack.budget_stop_policy_arn for p in attached):
                failures.append(f"S-0: BudgetStop is attached to {role}")
        try:
            self.month_meter(now)
        except Exception as exc:
            failures.append(f"S-0: the meter is broken: {_brief(exc)}")
        expected = {a.import_name for a in self.wave.arms if a.import_name}
        try:
            models = self.clients("bedrock").list_imported_models()["modelSummaries"]
        except Exception as exc:
            failures.append(f"S-0: cannot list imported models: {_brief(exc)}")
            models = []
        for model in models:
            name = model["modelName"]
            if name not in expected:
                failures.append(f"S-0: unexpected import {name}")
            elif all(self.arm_done(a.name) for a in self.wave.sharing_import(name)):
                failures.append(f"S-0: import {name} outlived its arms; delete it")
        if include_stop and self.state.hard_stop:
            failures.append(f"S-0: hard stop in force: {self.state.hard_stop}")
        return failures

    def arm_preflight(self, run: ArmRun) -> list[str]:
        """S-0 before an arm's first submission: its manifest, its pinned model config, and the
        configured pre-flight commands (the freeze-v3 record check)."""
        failures = []
        uploaded = self.store.get(run.sweep.manifest_key)
        if uploaded is not None and uploaded != run.sweep.body:
            failures.append(f"S-0: {run.sweep.manifest_key} differs from the manifest of {run.arm.sweep}")
        arm = run.arm
        entry = self.registry().get(arm.model) if arm.model else None
        if arm.model and not isinstance(entry, dict):
            failures.append(f"S-0: models.yaml has no entry {arm.model}")
        if arm.pinned_model is not None:
            for key, value in arm.pinned_model.items():
                found = (entry or {}).get(key)
                if found != value:
                    failures.append(f"S-0: models.yaml {arm.model}.{key} is {found!r}, pinned {value!r}")
        if arm.bedrock_model:
            profile_region = self.stack.profile_region(arm.bedrock_model)
            if profile_region is None:
                failures.append(f"S-0: the stack has no tagged profile for {arm.bedrock_model}")
            elif isinstance(entry, dict) and self.region_of(arm) != profile_region:
                failures.append(
                    f"S-0: models.yaml {arm.model} calls {self.region_of(arm)}, but the tagged profile "
                    f"for {arm.bedrock_model} is in {profile_region}; the meter would miss its tokens"
                )
        for command in self.wave.preflight_commands:
            code, output = self.run_command(command)
            if code != 0:
                failures.append(f"S-0: `{' '.join(command)}` exited {code}: {mask(output.strip())[-300:]}")
        return failures

    def run_gate(self, now: datetime) -> None:
        if self.wave.gate_command is None:
            self.state.gate = {"passed": True, "at": iso(now), "note": "no gate command configured"}
            return
        command = [self.substitute(part) for part in self.wave.gate_command]
        code, output = self.run_command(command)
        key = self.ledger.gate_output(now, output)
        self.state.gate = {"passed": code == 0, "at": iso(now), "exit": code, "output": key}
        self.ledger.event(now, "gate", passed=code == 0, exit=code)
        self.out(f"S-1 gate {'passed' if code == 0 else 'FAILED'} (exit {code})")
        if code != 0:
            self.hard_stop(f"S-1: scripted gate failed (exit {code}); see {key}", now)

    def substitute(self, part: str) -> str:
        part = part.replace("{bucket}", self.stack.bucket)
        for match in re.findall(r"\{manifest:([a-z0-9-]+)\}", part):
            run = self.arm_run(self.wave.arm(match))
            part = part.replace(f"{{manifest:{match}}}", f"s3://{self.stack.bucket}/{run.sweep.manifest_key}")
        return part

    # ---------------------------------------------------------------- starting and topping up

    def blockers(self, run: ArmRun, now: datetime) -> list[tuple[str, str]]:
        """Why an arm can't submit now, as (code, reason); empty when it can."""
        arm, out = run.arm, []
        if self.state.hard_stop:
            out.append(("stop", self.state.hard_stop))
        if arm.name in self.state.paused:
            out.append(("paused", self.state.paused[arm.name]))
        month = meter.month_key(now)
        rule = self.wave.months.get(month)
        if rule is None:
            out.append(("month", f"S-9: no approved budget for {month}"))
        elif rule.arms is not None and arm.name not in rule.arms:
            out.append(("month", f"{arm.name} may not run in {month}"))
        for cond in arm.start_after:
            if self.settled_fraction(cond) < cond.fraction:
                out.append(("order", f"waiting for {cond.arm} ({cond.fraction:.0%} settled)"))
        if arm.needs_gate and not (self.state.gate and self.state.gate.get("passed")):
            out.append(("gate", "S-1: waiting for the scripted gate"))
        if arm.month_fit and not out:
            reason = rules.s8_admitted(arm, self.wave.rules)
            if reason:
                out.append(("admission", reason))
            elif not run.started:
                decision = self.state.month_fit
                if decision is None:
                    result = self.month_fit(arm, now)
                    decision = {**result.as_dict(), "at": iso(now)}
                    self.state.month_fit = decision
                    self.ledger.event(now, "month_fit", **result.as_dict())
                    self.out(f"month fit for {arm.name}: {result.decision} ({result.reason})")
                if decision["decision"] != "run":
                    out.append(
                        ("month-fit", f"section 10.4: {decision['decision']}: {decision.get('reason', '')}")
                    )
        if arm.import_name and not out:
            state = rules.s5_warm(self.ledger.warm(arm.import_name), now, self.wave.rules)
            if state == "failed":
                out.append(("warm-failed", f"S-5: {arm.import_name} did not warm within 30 min; retry later"))
            elif state != "warm" and not run.started:
                out.append(("warm", f"waiting for {arm.import_name}'s first successful call"))
        return out

    def cap(self, run: ArmRun) -> int:
        arm = run.arm
        cap = arm.cap
        for rule in arm.cap_rules:
            if self.settled_fraction(rule.after) >= rule.after.fraction:
                cap = rule.cap
        cap -= self.state.cap_cut.get(arm.name, 0)
        if arm.ramp_step:
            level = self.state.ramp.get(arm.name, arm.ramp_step)
            if run.started and run.queued == 0 and run.running >= level and level < cap:
                level += arm.ramp_step
            self.state.ramp[arm.name] = level
            cap = min(cap, level)
        return max(0, cap)

    def start(self, run: ArmRun, now: datetime) -> bool:
        """First-submission checks: the October rule (S-9) and the arm's S-0. Opens the arm's window."""
        month = meter.month_key(now)
        reason = rules.s9_job_fits(month, self.wave.months, self.month_estimate(now), run.arm.p90_usd)
        failures = [reason] if reason else self.arm_preflight(run)
        if failures:
            self.pause(run.arm.name, "; ".join(failures), now)
            return False
        if self.store.get(run.sweep.manifest_key) is None:
            self.store.put(run.sweep.manifest_key, run.sweep.body)
        if not self.state.windows.get(run.arm.name):
            warm = self.ledger.warm(run.arm.import_name) if run.arm.import_name else None
            start = min([now, *([parse_iso(warm["started_at"])] if warm and warm.get("started_at") else [])])
            self.state.windows[run.arm.name] = [{"start": iso(start), "end": None}]
        self.ledger.event(now, "arm_start", arm=run.arm.name, episodes=run.total)
        return True

    def submit(self, run: ArmRun, offset: int, size: int, now: datetime) -> None:
        name = f"{self.job_prefix(run.arm)}{offset}"
        if self.dry_run:
            self._dry_jobs += 1
            job_id = f"dryrun-{self._dry_jobs}"
        else:
            kwargs: dict[str, Any] = {
                "jobName": name,
                "jobQueue": self.stack.queue,
                "jobDefinition": self.stack.job_definition,
                "parameters": {
                    "manifest": f"s3://{self.stack.bucket}/{run.sweep.manifest_key}",
                    "offset": str(offset),
                },
            }
            if size > 1:
                kwargs["arrayProperties"] = {"size": size}
            job_id = str(self.clients("batch").submit_job(**kwargs)["jobId"])
        record = JobRecord(
            run.arm.name, job_id, name, run.sweep.manifest_key, offset, size, iso(now), self.dry_run
        )
        self.ledger.record_job(record)
        run.jobs.append(record)
        run.live.update(record.indices)
        run.queued += size
        run.submissions.update(record.indices)

    def topup(self, run: ArmRun, room: int, now: datetime) -> int:
        free = min(self.cap(run) - len(run.live), room)
        if free <= 0:
            return 0
        open_ = run.open_indices(self.wave.max_submissions_per_episode)
        take = open_[:free]
        if not take or (len(take) < self.wave.topup_min and len(take) < len(open_)):
            return 0
        if not run.started and not self.start(run, now):
            return 0
        for offset, size in contiguous(take, self.wave.array_max):
            self.submit(run, offset, size, now)
        self.ledger.event(now, "topup", arm=run.arm.name, episodes=len(take), live=len(run.live))
        return len(take)

    # ---------------------------------------------------------------- imports

    def manage_imports(self, now: datetime) -> dict[str, dict[str, bool]]:
        flags: dict[str, dict[str, bool]] = {}
        for name in sorted({a.import_name for a in self.wave.arms if a.import_name}):
            sharing = self.wave.sharing_import(name)
            batch = [self.arm_run(a) for a in sharing if a.kind == "batch"]
            external_open = any(
                w.get("start") and not w.get("end")
                for a in sharing
                if a.kind == "external"
                for w in self.state.windows.get(a.name, [])
            )
            running = sum(r.running for r in batch)
            done = all(self.arm_done(a.name) for a in sharing) or name in self.state.deleted_imports
            if running or external_open:
                self.idle_since.pop(name, None)
                self.state.kill_pings.pop(name, None)
            else:
                self.idle_since.setdefault(name, now)
            arn = self.import_arn(name)
            if arn and name not in self.state.kill_pings and name not in self.state.deleted_imports:
                window = timedelta(seconds=self.wave.rules.idle_copy_s + 60)
                points = meter.metric_points(
                    self.cw(), "AWS/Bedrock", "ModelCopy", "ModelId", arn, now - window, now
                )
                if rules.s6_idle(points, self.idle_since.get(name), now, self.wave.rules):
                    self.state.kill_pings[name] = iso(now)
                    self.ledger.event(now, "kill_pings", import_name=name, rule="S-6")
                    self.out(f"S-6: {name} idle with a copy up; pings stopped")
            if done and name not in self.state.deleted_imports and arn:
                self.delete_import(name, now)
            ping = (
                not done
                and not self.state.hard_stop
                and name not in self.state.kill_pings
                and any(
                    not r.done and r.running == 0 and {c for c, _ in self.blockers(r, now)} <= {"warm"}
                    for r in batch
                )
            )
            flags[name] = {"ping": ping, "done": done}
        return flags

    def delete_import(self, name: str, now: datetime) -> None:
        if self.dry_run:
            self.out(f"dry run: would delete import {name} (its arms have settled)")
        else:
            self.clients("bedrock").delete_imported_model(modelIdentifier=name)
        self.state.deleted_imports[name] = iso(now)
        self.ledger.event(now, "import_deleted", import_name=name, dry_run=self.dry_run)

    # ---------------------------------------------------------------- the cycle

    def cycle(self) -> dict[str, Any]:
        now = self.clock()
        self.state = self.ledger.state()
        runs = [self.arm_run(a) for a in self.batch_arms()]
        for run in runs:
            self.refresh_jobs(run)
        self.scan(runs)
        self.apply_commands(now)
        month = meter.month_key(now)
        estimate = self.month_estimate(now)
        reason = rules.s9_stop(month, self.wave.months, estimate)
        if reason and month in self.wave.months:
            self.hard_stop(reason, now)
        for run in runs:
            if run.started:
                self.check_arm(run, now)
            if (
                run.done
                and self.state.windows.get(run.arm.name)
                and not self.state.windows[run.arm.name][-1]["end"]
            ):
                self.state.windows[run.arm.name][-1]["end"] = iso(now)
                self.ledger.event(
                    now,
                    "arm_settled",
                    arm=run.arm.name,
                    missing=sum(e.missing for e in run.episodes.values()),
                )
        gate_deps = {c.arm for a in self.wave.arms if a.needs_gate for c in a.start_after}
        gate_deps = {n for n in gate_deps if not self.wave.arm(n).needs_gate}
        if self.state.gate is None and gate_deps and all(self.arm_done(n) for n in gate_deps):
            self.run_gate(now)
        imports = self.manage_imports(now)
        room = self.wave.max_in_flight - sum(len(r.live) for r in runs)
        submitted: dict[str, int] = {}
        for run in runs:
            if run.done or self.blockers(run, now):
                continue
            n = self.topup(run, room, now)
            room -= n
            if n:
                submitted[run.arm.name] = n
        arms = {
            r.arm.name: {
                "total": r.total,
                "settled": r.settled,
                "live": len(r.live),
                "running": r.running,
                "paused": r.arm.name in self.state.paused,
            }
            for r in runs
        }
        self.ledger.heartbeat(
            {
                "at": iso(now),
                "state": "stopped" if self.state.hard_stop else "running",
                "dry_run": self.dry_run,
                "month_estimate_usd": round(estimate, 2),
                "arms": arms,
                "imports": imports,
            }
        )
        self.ledger.save_state(self.state)
        summary = {
            "at": iso(now),
            "month_estimate_usd": round(estimate, 2),
            "submitted": submitted,
            "arms": arms,
        }
        self.out(json.dumps(summary, sort_keys=True))
        return summary

    def finished(self) -> bool:
        return all(self.arm_run(a).done for a in self.batch_arms())


def _brief(exc: Exception) -> str:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
    return str(code) if code else mask(str(exc))[:200]


def _run_command(command: Sequence[str]) -> tuple[int, str]:
    done = subprocess.run(list(command), capture_output=True, text=True, check=False)
    return done.returncode, done.stdout + done.stderr
