#!/usr/bin/env python3
"""CDK entry point. Settings come from cdk.json context or `-c key=value`."""

import os

import aws_cdk as cdk

from lost_in_place_infra.sim_stack import SimSettings, SimStack

app = cdk.App()


def ctx(key: str, default: str) -> str:
    value = app.node.try_get_context(key)
    return default if value is None else str(value)


settings = SimSettings(
    budget_usd=float(ctx("budgetUsd", "100")),
    max_vcpus=int(ctx("maxVcpus", "32")),
    alert_email=ctx("alertEmail", ""),
    image_tag=ctx("imageTag", "latest"),
)
SimStack(
    app,
    "LostInPlaceSim",
    settings=settings,
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=ctx("region", "us-east-1")),
    description="lost-in-place: spot AWS Batch for PX4/Gazebo episodes with a budget stop",
)
cdk.Tags.of(app).add("project", "lost-in-place")
app.synth()
