"""The real-time cost meter (plan section 10.3; the AWS budget lags 18-24 h).

Three CloudWatch sources, each lagging about 1-5 minutes:
- tokens: `AWS/Bedrock` `InputTokenCount` / `OutputTokenCount` of the stack's tagged inference profiles
  (dimension `ModelId` = the profile id), times the arm's per-token price;
- import copies: `AWS/Bedrock` `ModelCopy` per imported model (one sample a minute, so its Sum is
  copy-minutes), times `copy_minute_usd`;
- compute: `AWS/Batch` `JobAttemptExecutionDuration` of the job queue (milliseconds per finished
  attempt) x `episode_vcpus` x `vcpu_hour_usd`, plus the running time so far of attempts still running.

Per arm, the same metrics are read over the arm's windows (warm-up or first submission to settlement)
and compute comes from the arm's own jobs. The episode traces give a second, independent token
count, used for S-8's rolling cost per episode and to check the meter (`validate`).

CloudWatch metrics stay in the region that served the call, so token metrics are read in each
profile's own region (Claude Sonnet 5.5 is called in us-east-2 from Batch in us-east-1); imports and
Batch are read in the wave's home region. `cw(region)` returns the CloudWatch client for a region
(None: the home region).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from lost_in_place.wave.config import Arm, Price, Wave

MAX_QUERIES = 500
Regional = Callable[[str | None], Any]


@dataclass
class Usage:
    input_tokens: float = 0.0
    output_tokens: float = 0.0
    token_usd: float = 0.0
    copy_minutes: float = 0.0
    copy_usd: float = 0.0
    batch_hours: float = 0.0
    compute_usd: float = 0.0
    other_usd: float = 0.0

    @property
    def total_usd(self) -> float:
        return self.token_usd + self.copy_usd + self.compute_usd + self.other_usd

    def add(self, other: Usage) -> Usage:
        return Usage(**{k: getattr(self, k) + getattr(other, k) for k in asdict(self)})

    def as_dict(self) -> dict[str, float]:
        return {**{k: round(v, 4) for k, v in asdict(self).items()}, "total_usd": round(self.total_usd, 4)}


@dataclass(frozen=True)
class Tokens:
    model_id: str  # CloudWatch ModelId: a tagged profile's id
    price: Price
    region: str | None = None  # where the profile serves calls (None: the home region)


@dataclass(frozen=True)
class Sources:
    tokens: tuple[Tokens, ...] = ()
    imports: tuple[str, ...] = ()  # imported-model ARNs (their CloudWatch ModelId)
    queue: str | None = None


def period_for(start: datetime, end: datetime, now: datetime) -> int:
    """CloudWatch keeps 1-minute points for 15 days and 5-minute points for 63 days."""
    age = now - start
    if age <= timedelta(days=14) and end - start <= timedelta(days=5):
        return 60
    if age <= timedelta(days=62):
        return 300
    return 3600


def metric_query(
    qid: str, namespace: str, metric: str, dimension: str, value: str, period: int
) -> dict[str, Any]:
    return {
        "Id": qid,
        "MetricStat": {
            "Metric": {
                "Namespace": namespace,
                "MetricName": metric,
                "Dimensions": [{"Name": dimension, "Value": value}],
            },
            "Period": period,
            "Stat": "Sum",
        },
        "ReturnData": True,
    }


def metric_sums(cw: Any, queries: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, float]:
    """{query id: sum of its points over [start, end)}."""
    totals: dict[str, float] = {q["Id"]: 0.0 for q in queries}
    for i in range(0, len(queries), MAX_QUERIES):
        token = None
        while True:
            kwargs: dict[str, Any] = {
                "MetricDataQueries": queries[i : i + MAX_QUERIES],
                "StartTime": start,
                "EndTime": end,
            }
            if token:
                kwargs["NextToken"] = token
            response = cw.get_metric_data(**kwargs)
            for result in response["MetricDataResults"]:
                totals[result["Id"]] += float(sum(result.get("Values", [])))
            token = response.get("NextToken")
            if not token:
                break
    return totals


def metric_points(
    cw: Any,
    namespace: str,
    metric: str,
    dimension: str,
    value: str,
    start: datetime,
    end: datetime,
    period: int = 60,
) -> list[tuple[datetime, float]]:
    response = cw.get_metric_data(
        MetricDataQueries=[metric_query("m", namespace, metric, dimension, value, period)],
        StartTime=start,
        EndTime=end,
    )
    result = response["MetricDataResults"][0]
    return sorted(
        zip(result.get("Timestamps", []), (float(v) for v in result.get("Values", [])), strict=True)
    )


def cloudwatch_usage(
    cw: Regional, wave: Wave, sources: Sources, start: datetime, end: datetime, now: datetime
) -> Usage:
    if end <= start:
        return Usage()
    period = period_for(start, end, now)
    by_region: dict[str | None, list[dict[str, Any]]] = {}
    for i, t in enumerate(sources.tokens):
        by_region.setdefault(t.region, []).extend(
            [
                metric_query(f"i{i}", "AWS/Bedrock", "InputTokenCount", "ModelId", t.model_id, period),
                metric_query(f"o{i}", "AWS/Bedrock", "OutputTokenCount", "ModelId", t.model_id, period),
            ]
        )
    home = by_region.setdefault(None, [])
    for i, arn in enumerate(sources.imports):
        home.append(metric_query(f"c{i}", "AWS/Bedrock", "ModelCopy", "ModelId", arn, period))
    if sources.queue:
        home.append(
            metric_query(
                "b", "AWS/Batch", "JobAttemptExecutionDuration", "JobQueueName", sources.queue, period
            )
        )
    sums: dict[str, float] = {}
    for region, queries in by_region.items():
        if queries:
            sums.update(metric_sums(cw(region), queries, start, end))
    if not sums:
        return Usage()
    usage = Usage()
    for i, t in enumerate(sources.tokens):
        usage.input_tokens += sums[f"i{i}"]
        usage.output_tokens += sums[f"o{i}"]
        usage.token_usd += t.price.cost(sums[f"i{i}"], sums[f"o{i}"])
    usage.copy_minutes = sum(sums[f"c{i}"] for i in range(len(sources.imports)))
    usage.copy_usd = usage.copy_minutes * wave.copy_minute_usd
    if sources.queue:
        usage.batch_hours = sums["b"] / 3.6e6
        usage.compute_usd = compute_usd(wave, usage.batch_hours * 3600)
    return usage


def compute_usd(wave: Wave, run_seconds: float) -> float:
    return run_seconds / 3600 * wave.episode_vcpus * wave.vcpu_hour_usd


def token_usd(price: Price | None, input_tokens: float, output_tokens: float) -> float:
    return price.cost(input_tokens, output_tokens) if price else 0.0


def project_sources(
    wave: Wave,
    profiles: dict[str, str],
    import_arns: Iterable[str],
    queue: str,
    regions: Mapping[str, str | None] | None = None,
) -> Sources:
    """Everything the project can spend through: every tagged profile of the stack (priced by the arm
    that calls it, or at the dearest configured price if none does, and read in `regions[profile]`),
    every imported model, the queue."""
    prices = {a.bedrock_model: a.price for a in wave.arms if a.bedrock_model and a.price}
    dearest = max(
        prices.values(),
        key=lambda p: p.input_per_mtok + p.output_per_mtok,
        default=Price(input_per_mtok=0, output_per_mtok=0),
    )
    regions = regions or {}
    tokens = tuple(
        Tokens(arn.rsplit("/", 1)[-1], prices.get(model, dearest), regions.get(model))
        for model, arn in sorted(profiles.items())
    )
    return Sources(tokens=tokens, imports=tuple(sorted(set(import_arns))), queue=queue)


def all_import_arns(cw: Any) -> list[str]:
    """Every imported model with a ModelCopy series in this region, deleted ones included. Imports are
    granted account-wide (infra/AGENTS.md), so this is the conservative set."""
    arns: set[str] = set()
    for page in cw.get_paginator("list_metrics").paginate(Namespace="AWS/Bedrock", MetricName="ModelCopy"):
        for metric in page.get("Metrics", []):
            for dim in metric.get("Dimensions", []):
                if dim["Name"] == "ModelId" and ":imported-model/" in dim["Value"]:
                    arns.add(dim["Value"])
    return sorted(arns)


def arm_sources(
    arm: Arm, profiles: dict[str, str], import_arns: dict[str, str], region: str | None = None
) -> Sources:
    tokens: tuple[Tokens, ...] = ()
    if arm.bedrock_model and arm.price:
        arn = profiles.get(arm.bedrock_model)
        if arn:
            tokens = (Tokens(arn.rsplit("/", 1)[-1], arm.price, region),)
    imports = (import_arns[arm.import_name],) if arm.import_name and arm.import_name in import_arns else ()
    return Sources(tokens=tokens, imports=imports)


def window_usage(
    cw: Regional,
    wave: Wave,
    sources: Sources,
    windows: list[tuple[datetime, datetime | None]],
    now: datetime,
) -> Usage:
    total = Usage()
    for start, end in windows:
        total = total.add(cloudwatch_usage(cw, wave, sources, start, end or now, now))
    return total


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_key(now: datetime) -> str:
    return now.strftime("%Y-%m")


def budget_actual(budgets: Any, account: str, budget_name: str) -> tuple[float, datetime]:
    """The AWS Budgets actual for this month and when it was last updated."""
    budget = budgets.describe_budget(AccountId=account, BudgetName=budget_name)["Budget"]
    spend = float(budget["CalculatedSpend"]["ActualSpend"]["Amount"])
    return spend, budget["LastUpdatedTime"]


@dataclass(frozen=True)
class Agreement:
    """One meter quantity measured two independent ways."""

    name: str
    meter: float
    cloudwatch: float

    @property
    def difference(self) -> float:
        return (self.meter - self.cloudwatch) / self.cloudwatch if self.cloudwatch else float("inf")

    def as_dict(self) -> dict[str, Any]:
        return {
            "quantity": self.name,
            "meter": round(self.meter, 4),
            "cloudwatch": round(self.cloudwatch, 4),
            "difference_pct": round(100 * self.difference, 2),
        }


def job_of(job_name: str, name_prefix: str) -> bool:
    """`e4-claude-40` is a job of `e4-claude-`; `e4-claude-pilot-40` is not."""
    return job_name.startswith(name_prefix) and job_name[len(name_prefix) :].isdigit()


def batch_attempts(batch: Any, queue: str, name_prefix: str) -> list[tuple[float, float]]:
    """(started, stopped) in epoch seconds of every finished attempt of the queue's jobs named
    `<name_prefix><offset>`, array children included."""
    pages = batch.get_paginator("list_jobs").paginate(
        jobQueue=queue, filters=[{"name": "JOB_NAME", "values": [f"{name_prefix}*"]}]
    )
    parents = [
        j for page in pages for j in page.get("jobSummaryList", []) if job_of(j["jobName"], name_prefix)
    ]
    ids: list[str] = []
    for job in parents:
        size = int((job.get("arrayProperties") or {}).get("size") or 0)
        ids += [f"{job['jobId']}:{k}" for k in range(size)] if size else [job["jobId"]]
    spans = []
    for i in range(0, len(ids), 100):
        for job in batch.describe_jobs(jobs=ids[i : i + 100])["jobs"]:
            for attempt in job.get("attempts", []):
                if attempt.get("startedAt") and attempt.get("stoppedAt"):
                    spans.append((attempt["startedAt"] / 1000, attempt["stoppedAt"] / 1000))
    return spans


def traced_tokens(episodes: Iterable[dict[str, Any]]) -> tuple[float, float]:
    """Input and output tokens recorded in episode traces (every attempt is billed, retries too)."""
    inputs = outputs = 0.0
    for episode in episodes:
        for call in episode.get("planner_decisions") or []:
            inputs += int(call.get("input_tokens") or 0)
            outputs += int(call.get("output_tokens") or 0)
    return inputs, outputs


def agreements(
    wave: Wave,
    price: Price,
    traced_tokens: tuple[float, float],
    job_seconds: float,
    cloudwatch: Usage,
) -> list[Agreement]:
    """The meter's episode-trace tokens and Batch job run time against CloudWatch's, in dollars."""
    traced_usd = price.cost(*traced_tokens)
    compute = compute_usd(wave, job_seconds)
    return [
        Agreement("input tokens", traced_tokens[0], cloudwatch.input_tokens),
        Agreement("output tokens", traced_tokens[1], cloudwatch.output_tokens),
        Agreement("token cost (USD)", traced_usd, cloudwatch.token_usd),
        Agreement("Batch run time (h)", job_seconds / 3600, cloudwatch.batch_hours),
        Agreement("total (USD)", traced_usd + compute, cloudwatch.token_usd + cloudwatch.compute_usd),
    ]
