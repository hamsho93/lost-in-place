"""In-memory stand-ins for the AWS clients the wave uses, and a small wave to run against them.

Every write call is appended to `Fakes.writes`, so tests can check that a dry run makes none.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from lost_in_place.wave import config
from lost_in_place.wave.aws import Clients, Stack
from lost_in_place.wave.store import LocalStore, Overlay, S3Store
from lost_in_place.wave.submit import Submitter

ROOT = Path(__file__).resolve().parents[1]
BUCKET = "results-bucket"
QUEUE = "the-queue"
PROFILE_ARN = "arn:aws:bedrock:us-east-1:000000000000:application-inference-profile/prof1"
EAST2_PROFILE_ARN = "arn:aws:bedrock:us-east-2:000000000000:application-inference-profile/prof2"
IMPORT_ARN = "arn:aws:bedrock:us-east-1:000000000000:imported-model/imp1"
OPERATOR_ARN = "arn:aws:iam::000000000000:role/LostInPlaceSim-OperatorRole1"
BUDGET_STOP_ARN = "arn:aws:iam::000000000000:policy/BudgetStop1"
STARTED_MS = int(datetime(2026, 11, 3, 11, 50, tzinfo=UTC).timestamp() * 1000)


class ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class Pages:
    def __init__(self, fn: Callable[..., Iterator[dict[str, Any]]]) -> None:
        self.fn = fn

    def paginate(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        return self.fn(**kwargs)


@dataclass
class FakeS3:
    writes: list[tuple[str, ...]]
    objects: dict[str, tuple[bytes, datetime]] = field(default_factory=dict)
    clock: Callable[[], datetime] = lambda: datetime(2026, 11, 3, tzinfo=UTC)

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.objects:
            raise ClientError("NoSuchKey")
        body = self.objects[Key][0]
        return {"Body": io.BytesIO(body), "ETag": str(hash(body))}

    def put_object(self, Bucket: str, Key: str, Body: bytes, **condition: str) -> None:
        self.writes.append(("s3.put_object", Key))
        if "IfNoneMatch" in condition and Key in self.objects:
            raise ClientError("PreconditionFailed")
        if "IfMatch" in condition and condition["IfMatch"] != str(hash(self.objects[Key][0])):
            raise ClientError("PreconditionFailed")
        self.objects[Key] = (Body, self.clock())

    def put(self, key: str, data: Any, modified: datetime | None = None) -> None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.objects[key] = (body, modified or self.clock())

    def get_paginator(self, name: str) -> Pages:
        def pages(Bucket: str, Prefix: str) -> Iterator[dict[str, Any]]:
            yield {
                "Contents": [
                    {"Key": k, "LastModified": m}
                    for k, (_, m) in sorted(self.objects.items())
                    if k.startswith(Prefix)
                ]
            }

        return Pages(pages)


@dataclass
class FakeBatch:
    writes: list[tuple[str, ...]]
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    child_status: dict[str, str] = field(default_factory=dict)
    submitted: list[dict[str, Any]] = field(default_factory=list)

    def submit_job(self, **kwargs: Any) -> dict[str, str]:
        self.writes.append(("batch.submit_job", kwargs["jobName"]))
        job_id = f"job-{len(self.submitted) + 1}"
        self.submitted.append(kwargs)
        size = int(kwargs.get("arrayProperties", {}).get("size", 1))
        self.jobs[job_id] = {
            "jobId": job_id,
            "jobName": kwargs["jobName"],
            "parameters": kwargs["parameters"],
            "size": size,
            "createdAt": STARTED_MS,
        }
        return {"jobId": job_id}

    def status(self, child_id: str) -> str:
        return self.child_status.get(child_id, "RUNNABLE")

    def describe_jobs(self, jobs: list[str]) -> dict[str, Any]:
        out = []
        for cid in jobs:
            parent = self.jobs[cid.split(":")[0]]
            status = self.status(cid)
            job: dict[str, Any] = {
                "jobId": cid,
                "jobName": parent["jobName"],
                "status": status,
                "parameters": parent["parameters"],
                "createdAt": parent["createdAt"],
            }
            if ":" not in cid and parent["size"] > 1:
                job["arrayProperties"] = {"size": parent["size"]}
            if status == "RUNNING":
                job["startedAt"] = STARTED_MS
            if status in ("SUCCEEDED", "FAILED"):
                job["attempts"] = [{"startedAt": STARTED_MS, "stoppedAt": STARTED_MS + 600_000}]
            out.append(job)
        return {"jobs": out}

    def get_paginator(self, name: str) -> Pages:
        def pages(jobQueue: str, filters: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
            prefix = filters[0]["values"][0].rstrip("*")
            yield {
                "jobSummaryList": [
                    {
                        "jobId": j["jobId"],
                        "jobName": j["jobName"],
                        "status": self.status(j["jobId"]),
                        **({"arrayProperties": {"size": j["size"]}} if j["size"] > 1 else {}),
                    }
                    for j in self.jobs.values()
                    if j["jobName"].startswith(prefix)
                ]
            }

        return Pages(pages)


@dataclass
class FakeCloudWatch:
    """`sums[(metric, model id or queue)]` is spread as one value per query; `points` feeds ModelCopy."""

    sums: dict[tuple[str, str], float] = field(default_factory=dict)
    points: dict[str, list[tuple[datetime, float]]] = field(default_factory=dict)
    calls: int = 0

    def get_metric_data(
        self, MetricDataQueries: list[dict[str, Any]], StartTime: datetime, EndTime: datetime, **_: Any
    ) -> dict[str, Any]:
        self.calls += 1
        results = []
        for q in MetricDataQueries:
            stat = q["MetricStat"]["Metric"]
            key = (stat["MetricName"], stat["Dimensions"][0]["Value"])
            if stat["MetricName"] == "ModelCopy" and key[1] in self.points:
                pts = [(t, v) for t, v in self.points[key[1]] if StartTime <= t < EndTime]
                results.append(
                    {"Id": q["Id"], "Timestamps": [t for t, _ in pts], "Values": [v for _, v in pts]}
                )
            else:
                value = self.sums.get(key, 0.0)
                results.append({"Id": q["Id"], "Timestamps": [StartTime], "Values": [value]})
        return {"MetricDataResults": results}

    def get_paginator(self, name: str) -> Pages:
        def pages(**_: Any) -> Iterator[dict[str, Any]]:
            yield {"Metrics": [{"Dimensions": [{"Name": "ModelId", "Value": IMPORT_ARN}]}]}

        return Pages(pages)


@dataclass
class FakeBedrock:
    writes: list[tuple[str, ...]]
    imports: dict[str, str] = field(default_factory=lambda: {"imp-1": IMPORT_ARN})

    def get_imported_model(self, modelIdentifier: str) -> dict[str, str]:
        if modelIdentifier not in self.imports:
            raise ClientError("ResourceNotFoundException")
        return {"modelArn": self.imports[modelIdentifier]}

    def list_imported_models(self) -> dict[str, Any]:
        return {"modelSummaries": [{"modelName": n} for n in self.imports]}

    def delete_imported_model(self, modelIdentifier: str) -> None:
        self.writes.append(("bedrock.delete_imported_model", modelIdentifier))
        self.imports.pop(modelIdentifier, None)


@dataclass
class FakeRuntime:
    writes: list[tuple[str, ...]]
    not_ready: int = 0
    calls: list[str] = field(default_factory=list)

    def invoke_model(self, modelId: str, **_: Any) -> dict[str, Any]:
        self.calls.append(modelId)
        if self.not_ready > 0:
            self.not_ready -= 1
            raise ClientError("ModelNotReadyException")
        return {"body": io.BytesIO(b'{"outputs": [{"text": "pong"}]}')}


@dataclass
class FakeBudgets:
    actual: float = 0.0
    updated: datetime = datetime(2026, 11, 3, tzinfo=UTC)
    action_status: str = "STANDBY"

    def describe_budget(self, AccountId: str, BudgetName: str) -> dict[str, Any]:
        return {
            "Budget": {
                "CalculatedSpend": {"ActualSpend": {"Amount": str(self.actual)}},
                "LastUpdatedTime": self.updated,
            }
        }

    def describe_budget_actions_for_budget(self, AccountId: str, BudgetName: str) -> dict[str, Any]:
        return {"Actions": [{"Status": self.action_status}]}


@dataclass
class FakeIAM:
    attached: list[str] = field(default_factory=list)

    def list_attached_role_policies(self, RoleName: str) -> dict[str, Any]:
        return {"AttachedPolicies": [{"PolicyArn": a} for a in self.attached]}


class FakeCFN:
    def describe_stacks(self, StackName: str) -> dict[str, Any]:
        return {
            "Stacks": [
                {
                    "Outputs": [
                        {"OutputKey": "ResultsBucket", "OutputValue": BUCKET},
                        {"OutputKey": "JobQueue", "OutputValue": QUEUE},
                        {"OutputKey": "SweepJobDefinition", "OutputValue": "sweep-def:1"},
                        {"OutputKey": "OperatorRoleArn", "OutputValue": OPERATOR_ARN},
                        {
                            "OutputKey": "BedrockProfiles",
                            "OutputValue": json.dumps({"us.test.model": PROFILE_ARN}),
                        },
                    ]
                }
            ]
        }

    def describe_stack_resources(self, StackName: str) -> dict[str, Any]:
        return {
            "StackResources": [
                {
                    "ResourceType": "AWS::Budgets::Budget",
                    "LogicalResourceId": "MonthlyBudget",
                    "PhysicalResourceId": "b",
                },
                {
                    "ResourceType": "AWS::IAM::ManagedPolicy",
                    "LogicalResourceId": "BudgetStop1",
                    "PhysicalResourceId": BUDGET_STOP_ARN,
                },
                {
                    "ResourceType": "AWS::IAM::Role",
                    "LogicalResourceId": "JobRole1",
                    "PhysicalResourceId": "Lip-JobRole",
                },
            ]
        }


class FakeSTS:
    def get_caller_identity(self) -> dict[str, str]:
        return {
            "Account": "000000000000",
            "Arn": "arn:aws:sts::000000000000:assumed-role/LostInPlaceSim-OperatorRole1/s",
        }


@dataclass
class Fakes:
    writes: list[tuple[str, ...]] = field(default_factory=list)
    s3: FakeS3 = field(init=False)
    batch: FakeBatch = field(init=False)
    cloudwatch: FakeCloudWatch = field(default_factory=FakeCloudWatch)
    cloudwatch_east2: FakeCloudWatch = field(default_factory=FakeCloudWatch)
    bedrock: FakeBedrock = field(init=False)
    runtime: FakeRuntime = field(init=False)
    budgets: FakeBudgets = field(default_factory=FakeBudgets)
    iam: FakeIAM = field(default_factory=FakeIAM)

    def __post_init__(self) -> None:
        self.s3 = FakeS3(self.writes)
        self.batch = FakeBatch(self.writes)
        self.bedrock = FakeBedrock(self.writes)
        self.runtime = FakeRuntime(self.writes)

    def __call__(self, service: str, region: str | None = None) -> Any:
        if region not in (None, "us-east-1", "us-east-2"):
            raise ValueError(f"no fake {service} in {region}")
        if service == "cloudwatch" and region == "us-east-2":
            return self.cloudwatch_east2
        return {
            "s3": self.s3,
            "batch": self.batch,
            "cloudwatch": self.cloudwatch,
            "bedrock": self.bedrock,
            "bedrock-runtime": self.runtime,
            "budgets": self.budgets,
            "iam": self.iam,
            "cloudformation": FakeCFN(),
            "sts": FakeSTS(),
        }[service]


STACK = Stack(
    name="LostInPlaceSim",
    bucket=BUCKET,
    queue=QUEUE,
    job_definition="sweep-def:1",
    operator_role_arn=OPERATOR_ARN,
    bedrock_profiles={"us.test.model": PROFILE_ARN},
    budget_name="b",
    budget_stop_policy_arn=BUDGET_STOP_ARN,
    job_role_name="Lip-JobRole",
)

SWEEP = {
    "scenarios": ["scenarios/e1/aisle_*.yaml", "scenarios/e1/bay_*.yaml"],
    "reps": 3,
    "validators": ["none"],
    "observations": ["O0", "O1"],
    "latency_s": 2.0,
    "shuffle_seed": 0,
}


def wave_dict(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "e5",
        "n_families": 2,
        "ledger_prefix": "manifests/e5/ledger",
        "queries_log": "manifests/e5/queries.log",
        "topup_min": 1,
        "array_max": 5,
        "max_in_flight": 64,
        "months": {"2026-11": {"stop_usd": 400}},
        "arms": [
            {
                "name": "scripted-gate",
                "sweep": "sweeps/e5/scripted.yaml",
                "select": {"scenario_regex": "_(loud|healthy)$"},
                "cap": 7,
                "cap_usd": 10,
            },
            {
                "name": "claude",
                "sweep": "sweeps/e5/claude.yaml",
                "cap": 5,
                "needs_gate": True,
                "start_after": [{"arm": "scripted-gate"}],
                "bedrock_model": "us.test.model",
                "price": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
                "cap_usd": 95,
                "estimate_usd": 79,
                "p90_usd": 90,
            },
            {
                "name": "student",
                "sweep": "sweeps/e5/student.yaml",
                "cap": 4,
                "needs_gate": True,
                "start_after": [{"arm": "scripted-gate"}],
                "import_name": "imp-1",
                "cap_usd": 50,
                "estimate_usd": 38,
                "p90_usd": 45,
            },
        ],
    }
    data.update(overrides)
    return data


def write_sweeps(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "scenarios").symlink_to(ROOT / "scenarios")
    (root / "sweeps/e5").mkdir(parents=True)
    for name, planner in (
        ("scripted", "scripted-naive"),
        ("claude", "scripted-naive"),
        ("student", "scripted-naive"),
        ("gpt", "scripted-naive"),
        ("sonnet", "scripted-naive"),
    ):
        spec = {"name": f"e5-{name}", "planners": [planner], **SWEEP}
        (root / f"sweeps/e5/{name}.yaml").write_text(yaml.safe_dump(spec))


@dataclass
class Clock:
    now: datetime = datetime(2026, 11, 3, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@dataclass
class World:
    root: Path
    fakes: Fakes
    clock: Clock
    wave: config.Wave
    dry_run: bool = False
    commands: list[list[str]] = field(default_factory=list)
    gate_exit: int = 0
    output: list[str] = field(default_factory=list)
    stack: Stack = field(default_factory=lambda: STACK)

    def run_command(self, command: Any) -> tuple[int, str]:
        self.commands.append(list(command))
        return (self.gate_exit if "--gates" in command else 0), "ok"

    def submitter(self) -> Submitter:
        clients = Clients(self.fakes, read_only=self.dry_run)
        s3 = S3Store(clients("s3"), BUCKET, sleep=lambda _: None)
        store = Overlay(s3, LocalStore(self.root / "local")) if self.dry_run else s3
        return Submitter(
            self.wave,
            clients,
            self.stack,
            store,
            root=self.root,
            dry_run=self.dry_run,
            clock=self.clock,
            run_command=self.run_command,
            out=self.output.append,
        )

    def sweep_entries(self, arm: str) -> list[Any]:
        sub = self.submitter()
        return sub.arm_run(self.wave.arm(arm)).sweep.entries

    def result(
        self,
        sweep: str,
        key: str,
        label: str = "a-1-attempt1",
        outcome: str = "completed",
        score: dict[str, Any] | None = None,
        tokens: tuple[int, int] = (1000, 100),
        modified: datetime | None = None,
    ) -> None:
        prefix = f"runs/{sweep}/{key}/{label}"
        episode = {
            "outcome": outcome,
            "planner_decisions": [{"input_tokens": tokens[0], "output_tokens": tokens[1]}],
            "wall_s": {"stopped": 250.0},
        }
        when = modified or self.clock.now - timedelta(hours=1)
        self.fakes.s3.put(f"{prefix}/episode.json", episode, when)
        if score is not None:
            self.fakes.s3.put(f"{prefix}/score.json", score, when)

    def settle_all(self, arm: str, sweep: str) -> None:
        sub = self.submitter()
        run = sub.arm_run(self.wave.arm(arm))
        for i in run.indices:
            self.result(sweep, run.sweep.entries[i].key, score=good_score())


def good_score(**extra: Any) -> dict[str, Any]:
    return {
        "n_planner_calls": 10,
        "n_invalid": 0,
        "manipulation_ok": True,
        "manipulation_decision_ok": True,
        "onset_to_first_decision_s": 2.5,
        "first_move_length_m": 1.5,
        "violation": False,
        **extra,
    }


def make_world(tmp_path: Path, dry_run: bool = False, **overrides: Any) -> World:
    write_sweeps(tmp_path)
    return World(tmp_path, Fakes(), Clock(), config.Wave.model_validate(wave_dict(**overrides)), dry_run)


def jobs_by_name(fakes: Fakes, arm: str) -> list[dict[str, Any]]:
    return [j for j in fakes.batch.submitted if re.match(rf"e5-{arm}-\d+$", j["jobName"])]
