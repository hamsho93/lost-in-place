"""The cost meter: CloudWatch sums to dollars, Batch attempt time, trace tokens and the agreement check."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from wave_fakes import EAST2_PROFILE_ARN, IMPORT_ARN, PROFILE_ARN, QUEUE, FakeBatch, Fakes, make_world

from lost_in_place.wave import config, meter
from lost_in_place.wave.config import Price

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
PRICE = Price(input_per_mtok=3.3, output_per_mtok=16.5)


def test_cloudwatch_usage_prices_tokens_copies_and_compute() -> None:
    fakes = Fakes()
    profile = PROFILE_ARN.rsplit("/", 1)[-1]
    fakes.cloudwatch.sums.update(
        {
            ("InputTokenCount", profile): 1e6,
            ("OutputTokenCount", profile): 1e5,
            ("ModelCopy", IMPORT_ARN): 0.0,
            ("JobAttemptExecutionDuration", QUEUE): 3.6e6 * 10,
        }
    )
    fakes.cloudwatch.points[IMPORT_ARN] = [(NOW - timedelta(minutes=m), 1.0) for m in range(1, 101)]
    wave = config.load()
    sources = meter.Sources((meter.Tokens(profile, PRICE),), (IMPORT_ARN,), QUEUE)
    usage = meter.cloudwatch_usage(
        lambda region: fakes("cloudwatch", region), wave, sources, NOW - timedelta(hours=3), NOW, NOW
    )
    assert round(usage.token_usd, 4) == round(3.3 + 1.65, 4)
    assert usage.copy_minutes == 100 and round(usage.copy_usd, 2) == 11.40
    assert usage.batch_hours == 10 and round(usage.compute_usd, 3) == 0.5
    assert round(usage.total_usd, 2) == round(3.3 + 1.65 + 11.4 + 0.5, 2)


def test_token_metrics_are_read_in_each_profiles_region() -> None:
    fakes = Fakes()
    east1, east2 = PROFILE_ARN.rsplit("/", 1)[-1], EAST2_PROFILE_ARN.rsplit("/", 1)[-1]
    fakes.cloudwatch.sums[("InputTokenCount", east1)] = 1e6
    fakes.cloudwatch.sums[("InputTokenCount", east2)] = 7e6
    fakes.cloudwatch_east2.sums[("InputTokenCount", east2)] = 2e6
    fakes.cloudwatch_east2.sums[("JobAttemptExecutionDuration", QUEUE)] = 3.6e9
    sonnet = Price(input_per_mtok=2.2, output_per_mtok=11)
    sources = meter.Sources(
        (meter.Tokens(east1, PRICE), meter.Tokens(east2, sonnet, "us-east-2")), queue=QUEUE
    )
    usage = meter.cloudwatch_usage(
        lambda region: fakes("cloudwatch", region), config.load(), sources, NOW - timedelta(hours=1), NOW, NOW
    )
    assert usage.input_tokens == 3e6 and round(usage.token_usd, 4) == round(3.3 + 2 * 2.2, 4)
    assert usage.batch_hours == 0
    assert fakes.cloudwatch.calls == 1 and fakes.cloudwatch_east2.calls == 1


def test_period_follows_cloudwatch_retention() -> None:
    assert meter.period_for(NOW - timedelta(days=1), NOW, NOW) == 60
    assert meter.period_for(NOW - timedelta(days=20), NOW - timedelta(days=19), NOW) == 300
    assert meter.period_for(NOW - timedelta(days=70), NOW, NOW) == 3600


def test_metric_sums_batches_queries_and_follows_pages() -> None:
    class Paged:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def get_metric_data(
            self, MetricDataQueries: list[Any], NextToken: str | None = None, **_: Any
        ) -> Any:
            self.calls.append(len(MetricDataQueries))
            results = [{"Id": q["Id"], "Values": [1.0]} for q in MetricDataQueries]
            return {"MetricDataResults": results, **({} if NextToken else {"NextToken": "t"})}

    cw = Paged()
    queries = [
        meter.metric_query(f"q{i}", "AWS/Bedrock", "Invocations", "ModelId", "m", 60) for i in range(501)
    ]
    sums = meter.metric_sums(cw, queries, NOW - timedelta(hours=1), NOW)
    assert cw.calls == [500, 500, 1, 1]
    assert sums["q0"] == 2.0 and sums["q500"] == 2.0


def test_batch_attempts_expand_arrays_and_ignore_other_sweeps() -> None:
    batch = FakeBatch([])
    for name, size in (("e4-claude-0", 3), ("e4-claude-3", 1), ("e4-claude-pilot-0", 2)):
        batch.submit_job(
            jobName=name, parameters={}, **({"arrayProperties": {"size": size}} if size > 1 else {})
        )
    for cid in ("job-1:0", "job-1:1", "job-1:2", "job-2", "job-3:0", "job-3:1"):
        batch.child_status[cid] = "SUCCEEDED"
    spans = meter.batch_attempts(batch, QUEUE, "e4-claude-")
    assert len(spans) == 4 and all(round(e - s) == 600 for s, e in spans)


def test_traced_tokens_count_every_attempt() -> None:
    episodes = [
        {"planner_decisions": [{"input_tokens": 100, "output_tokens": 10}, {"input_tokens": 50}]},
        {"planner_decisions": None},
        {"planner_decisions": [{"input_tokens": 1, "output_tokens": 2, "error": "x"}]},
    ]
    assert meter.traced_tokens(episodes) == (151, 12)


def test_agreement_is_relative_to_cloudwatch() -> None:
    wave = config.load()
    cw = meter.Usage(input_tokens=1_000_000, output_tokens=100_000, batch_hours=10)
    cw.token_usd = PRICE.cost(cw.input_tokens, cw.output_tokens)
    cw.compute_usd = meter.compute_usd(wave, 36000)
    found = {a.name: a for a in meter.agreements(wave, PRICE, (990_000, 99_000), 36000, cw)}
    assert round(100 * found["input tokens"].difference, 3) == -1.0
    assert round(100 * found["token cost (USD)"].difference, 3) == -1.0
    assert found["Batch run time (h)"].difference == 0
    assert found["total (USD)"].as_dict()["difference_pct"] < 0


def test_month_meter_counts_every_import_and_running_attempts(tmp_path: Any) -> None:
    world = make_world(tmp_path)
    world.fakes.cloudwatch.points[IMPORT_ARN] = [(NOW - timedelta(minutes=m), 1.0) for m in range(1, 61)]
    sub = world.submitter()
    usage = sub.month_meter(NOW)
    assert usage.copy_minutes == 60
    assert round(usage.other_usd, 3) == round(0.5 * 2.5, 3)
