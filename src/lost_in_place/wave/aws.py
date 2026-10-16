"""AWS access for the wave: the stack's resources, found at run time, and clients under OperatorRole.

Account-specific values (bucket, queue, role ARN, inference profiles) are read from the stack's
CloudFormation outputs, or from environment variables that override them, never from the repo.
Clients run under OperatorRole with credentials that refresh themselves before they expire, so a
submitter left running for a day keeps working. In a dry run every client is read-only.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

SERVICES = (
    "s3",
    "batch",
    "cloudwatch",
    "bedrock",
    "bedrock-runtime",
    "budgets",
    "iam",
    "cloudformation",
    "sts",
)
READ_PREFIXES = ("get_", "list_", "describe_", "head_", "can_paginate", "meta", "exceptions")
ENV_OVERRIDES = {
    "ResultsBucket": "LIP_RESULTS_BUCKET",
    "JobQueue": "LIP_JOB_QUEUE",
    "OperatorRoleArn": "LIP_OPERATOR_ROLE_ARN",
    "BedrockProfiles": "LIP_BEDROCK_PROFILES",
}
ACCOUNT_ID = re.compile(r"\b\d{12}\b")


def mask(text: str) -> str:
    return ACCOUNT_ID.sub("<account>", text)


class WriteRefused(RuntimeError):
    """A dry run tried to change something in AWS."""


class ReadOnly:
    """Wraps a client so that only read calls go through."""

    def __init__(self, client: Any, service: str) -> None:
        self._client = client
        self._service = service

    def __getattr__(self, name: str) -> Any:
        if name.startswith(READ_PREFIXES):
            return getattr(self._client, name)
        raise WriteRefused(f"dry run: {self._service}.{name} is not a read call")


class Clients:
    """One client per service, created on first use. `factory(service)` builds a client; tests pass
    fakes. With `read_only`, every write call raises `WriteRefused`."""

    def __init__(self, factory: Callable[[str], Any], *, read_only: bool = False) -> None:
        self._factory = factory
        self._clients: dict[str, Any] = {}
        self.read_only = read_only

    def __call__(self, service: str) -> Any:
        if service not in self._clients:
            client = self._factory(service)
            self._clients[service] = ReadOnly(client, service) if self.read_only else client
        return self._clients[service]


@dataclass(frozen=True)
class Stack:
    name: str
    bucket: str
    queue: str
    job_definition: str
    operator_role_arn: str
    bedrock_profiles: dict[str, str] = field(default_factory=dict)
    budget_name: str = ""
    budget_stop_policy_arn: str = ""
    job_role_name: str = ""

    @property
    def operator_role_name(self) -> str:
        return self.operator_role_arn.rsplit("/", 1)[-1]

    def profile_id(self, system_profile: str) -> str | None:
        """The CloudWatch `ModelId` of the stack's tagged copy of a system inference profile."""
        arn = self.bedrock_profiles.get(system_profile)
        return arn.rsplit("/", 1)[-1] if arn else None


def discover(
    cfn: Any, stack_name: str, job_definition_output: str, env: Mapping[str, str] = os.environ
) -> Stack:
    """The stack's resources from its outputs and resources, with environment overrides."""
    described = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in described.get("Outputs", [])}
    for output, variable in ENV_OVERRIDES.items():
        if env.get(variable):
            outputs[output] = env[variable]
    if env.get("LIP_JOB_DEFINITION"):
        outputs[job_definition_output] = env["LIP_JOB_DEFINITION"]
    missing = [
        k for k in ("ResultsBucket", "JobQueue", job_definition_output, "OperatorRoleArn") if k not in outputs
    ]
    if missing:
        raise LookupError(f"stack {stack_name} has no output(s) {', '.join(missing)}")
    resources = cfn.describe_stack_resources(StackName=stack_name)["StackResources"]

    def physical(kind: str, logical_prefix: str) -> str:
        for r in resources:
            if r["ResourceType"] == kind and r["LogicalResourceId"].startswith(logical_prefix):
                return str(r["PhysicalResourceId"])
        return ""

    return Stack(
        name=stack_name,
        bucket=outputs["ResultsBucket"],
        queue=outputs["JobQueue"],
        job_definition=outputs[job_definition_output],
        operator_role_arn=outputs["OperatorRoleArn"],
        bedrock_profiles=dict(json.loads(outputs.get("BedrockProfiles") or "{}")),
        budget_name=physical("AWS::Budgets::Budget", "MonthlyBudget") or f"{stack_name}-monthly",
        budget_stop_policy_arn=physical("AWS::IAM::ManagedPolicy", "BudgetStop"),
        job_role_name=physical("AWS::IAM::Role", "JobRole"),
    )


def boto3_factory(
    region: str, role_arn: str | None = None, session_name: str = "lip-e5-wave"
) -> Callable[[str], Any]:
    """Client factory for the given region. With `role_arn`, clients use that role through
    credentials that refresh 15 minutes before they expire (OperatorRole sessions last up to 12 h)."""
    import boto3
    import botocore.session
    from botocore.config import Config
    from botocore.credentials import RefreshableCredentials

    base = boto3.Session(region_name=region)
    if role_arn:
        sts = base.client("sts")

        def refresh() -> dict[str, str]:
            creds = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name, DurationSeconds=3600)[
                "Credentials"
            ]
            return {
                "access_key": creds["AccessKeyId"],
                "secret_key": creds["SecretAccessKey"],
                "token": creds["SessionToken"],
                "expiry_time": creds["Expiration"].isoformat(),
            }

        core = botocore.session.get_session()
        core._credentials = RefreshableCredentials.create_from_metadata(
            metadata=refresh(), refresh_using=refresh, method="sts-assume-role"
        )
        core.set_config_variable("region", region)
        session = boto3.Session(botocore_session=core, region_name=region)
    else:
        session = base
    config = Config(retries={"mode": "standard", "max_attempts": 5})

    def factory(service: str) -> Any:
        if service == "budgets":
            return session.client(service, region_name="us-east-1", config=config)
        return session.client(service, config=config)

    return factory


def account_id(sts: Any) -> str:
    return str(sts.get_caller_identity()["Account"])


def utcnow() -> datetime:
    return datetime.now(UTC)
