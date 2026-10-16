#!/usr/bin/env python3
"""CDK entry point. Settings come from cdk.json context or `-c key=value`."""

import json
import os
from pathlib import Path

import aws_cdk as cdk

from lost_in_place_infra.sim_stack import (
    SimSettings,
    SimStack,
    bedrock_models_from_registry,
    bedrock_regions_from_registry,
    imported_models_from_registry,
)

MODEL_REGISTRY = Path(__file__).resolve().parent.parent / "src" / "lost_in_place" / "llm" / "models.yaml"

app = cdk.App()


def ctx(key: str, default: str) -> str:
    value = app.node.try_get_context(key)
    return default if value is None else str(value)


def ctx_map(key: str) -> dict[str, str]:
    value = app.node.try_get_context(key)  # a JSON string from -c, an object from cdk.json
    return dict(json.loads(value) if isinstance(value, str) else value or {})


settings = SimSettings(
    budget_usd=float(ctx("budgetUsd", "100")),
    max_vcpus=int(ctx("maxVcpus", "128")),
    alert_email=ctx("alertEmail", ""),
    image_tag=ctx("imageTag", "latest"),
    account_alarm_usd=float(ctx("accountAlarmUsd", "0")),
    github_repo=ctx("githubRepo", ""),
    bedrock_models=bedrock_models_from_registry(MODEL_REGISTRY),
    bedrock_regions=bedrock_regions_from_registry(MODEL_REGISTRY),
    external_profiles=ctx_map("useast2Profiles"),
    imported_models=imported_models_from_registry(MODEL_REGISTRY),
)
SimStack(
    app,
    "LostInPlaceSim",
    settings=settings,
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=ctx("region", "us-east-1")),
    description="lost-in-place: spot AWS Batch for PX4/Gazebo episodes with a budget stop",
)
app.synth()
