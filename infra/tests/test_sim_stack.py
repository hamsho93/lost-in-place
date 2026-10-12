import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from lost_in_place_infra.sim_stack import (
    GITHUB_OIDC_HOST,
    SimSettings,
    SimStack,
    bedrock_models_from_registry,
)


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = SimStack(
        app,
        "Test",
        settings=SimSettings(budget_usd=50, max_vcpus=16, alert_email="a@example.com"),
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    return Template.from_stack(stack)


def test_compute_is_spot_x86_and_capped(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Batch::ComputeEnvironment",
        {
            "ComputeResources": Match.object_like(
                {
                    "Type": "SPOT",
                    "AllocationStrategy": "SPOT_PRICE_CAPACITY_OPTIMIZED",
                    "MinvCpus": 0,
                    "MaxvCpus": 16,
                }
            )
        },
    )


def test_compute_has_no_two_vcpu_instances(template: Template) -> None:
    (compute,) = template.find_resources("AWS::Batch::ComputeEnvironment").values()
    types = compute["Properties"]["ComputeResources"]["InstanceTypes"]
    assert sorted(types) == sorted(
        f"{family}.{size}" for family in ("c7i", "c7a", "m7i", "c6i") for size in ("xlarge", "2xlarge")
    )


def test_job_retries_spot_reclaims_and_times_out(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Batch::JobDefinition",
        {
            "RetryStrategy": Match.object_like({"Attempts": 3}),
            "Timeout": {"AttemptDurationSeconds": 1800},
        },
    )


def test_budget_alerts_and_stop_action(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Budgets::Budget",
        {
            "Budget": Match.object_like(
                {
                    "BudgetLimit": {"Amount": 50, "Unit": "USD"},
                    "TimeUnit": "MONTHLY",
                    "CostFilters": {"TagKeyValue": ["user:project$lost-in-place"]},
                }
            ),
            "NotificationsWithSubscribers": Match.array_with(
                [
                    Match.object_like(
                        {
                            "Notification": Match.object_like(
                                {"NotificationType": "FORECASTED", "Threshold": 100}
                            )
                        }
                    )
                ]
            ),
        },
    )
    template.has_resource_properties(
        "AWS::Budgets::BudgetsAction",
        {
            "ActionType": "APPLY_IAM_POLICY",
            "ApprovalModel": "AUTOMATIC",
            "ActionThreshold": {"Type": "PERCENTAGE", "Value": 100},
        },
    )
    template.has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "a@example.com"}
    )


def test_budget_action_role_uses_the_managed_policy_path(template: Template) -> None:
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": Match.object_like(
                {"Statement": [Match.object_like({"Principal": {"Service": "budgets.amazonaws.com"}})]}
            ),
            "ManagedPolicyArns": [
                {
                    "Fn::Join": [
                        "",
                        [
                            "arn:",
                            {"Ref": "AWS::Partition"},
                            ":iam::aws:policy/AWSBudgetsActionsWithAWSResourceControlAccess",
                        ],
                    ]
                }
            ],
        },
    )


def test_results_bucket_is_private_and_encrypted(template: Template) -> None:
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
            "BucketEncryption": Match.any_value(),
        },
    )
    template.has_resource_properties(
        "AWS::S3::BucketPolicy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Effect": "Deny",
                                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                            }
                        )
                    ]
                )
            }
        },
    )


def test_no_nat_gateway(template: Template) -> None:
    template.resource_count_is("AWS::EC2::NatGateway", 0)


def test_spot_instances_carry_the_budget_tag(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Batch::ComputeEnvironment",
        {"ComputeResources": Match.object_like({"Tags": {"project": "lost-in-place"}})},
    )


def test_no_account_wide_billing_alarm_by_default(template: Template) -> None:
    template.resource_count_is("AWS::CloudWatch::Alarm", 0)


def test_account_wide_billing_alarm_is_opt_in() -> None:
    app = cdk.App()
    stack = SimStack(
        app,
        "WithAlarm",
        settings=SimSettings(account_alarm_usd=400),
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    Template.from_stack(stack).has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {"Namespace": "AWS/Billing", "MetricName": "EstimatedCharges", "Threshold": 400},
    )


def test_no_image_push_role_without_github_repo(template: Template) -> None:
    roles = template.find_resources(
        "AWS::IAM::Role",
        {
            "Properties": {
                "AssumeRolePolicyDocument": {
                    "Statement": [Match.object_like({"Action": "sts:AssumeRoleWithWebIdentity"})]
                }
            }
        },
    )
    assert roles == {}


def test_image_push_role_trusts_only_main_of_the_repo() -> None:
    app = cdk.App()
    stack = SimStack(
        app,
        "WithPush",
        settings=SimSettings(github_repo="owner/repo"),
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    template = Template.from_stack(stack)
    template.resource_count_is("AWS::IAM::OIDCProvider", 0)
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": Match.object_like(
                {
                    "Statement": [
                        Match.object_like(
                            {
                                "Action": "sts:AssumeRoleWithWebIdentity",
                                "Condition": {
                                    "StringEquals": {
                                        f"{GITHUB_OIDC_HOST}:aud": "sts.amazonaws.com",
                                        f"{GITHUB_OIDC_HOST}:sub": "repo:owner/repo:ref:refs/heads/main",
                                    }
                                },
                            }
                        )
                    ]
                }
            ),
            "MaxSessionDuration": 3600,
        },
    )
    template.has_output("ImagePushRoleArn", Match.any_value())


def test_image_push_role_accepts_immutable_subject_repo() -> None:
    app = cdk.App()
    stack = SimStack(
        app,
        "WithImmutablePush",
        settings=SimSettings(github_repo="owner@1/repo@2"),
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    Template.from_stack(stack).has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": Match.object_like(
                {
                    "Statement": [
                        Match.object_like(
                            {
                                "Condition": {
                                    "StringEquals": Match.object_like(
                                        {f"{GITHUB_OIDC_HOST}:sub": "repo:owner@1/repo@2:ref:refs/heads/main"}
                                    )
                                }
                            }
                        )
                    ]
                }
            )
        },
    )


SONNET = "us.anthropic.claude-sonnet-4-6"


@pytest.fixture(scope="module")
def bedrock_template() -> Template:
    app = cdk.App()
    stack = SimStack(
        app,
        "WithBedrock",
        settings=SimSettings(bedrock_models=(SONNET,)),
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    return Template.from_stack(stack)


def test_no_bedrock_resources_without_models(template: Template) -> None:
    template.resource_count_is("AWS::Bedrock::ApplicationInferenceProfile", 0)
    assert "bedrock:InvokeModel" not in json.dumps(template.find_resources("AWS::IAM::Policy"))


def test_planner_models_get_a_tagged_application_profile(bedrock_template: Template) -> None:
    [profile] = bedrock_template.find_resources("AWS::Bedrock::ApplicationInferenceProfile").values()
    props = profile["Properties"]
    assert props["InferenceProfileName"] == "WithBedrock-anthropic-claude-sonnet-4-6"
    assert json.dumps(props["ModelSource"]["CopyFrom"]).endswith(f':inference-profile/{SONNET}"]]}}')
    assert {"Key": "project", "Value": "lost-in-place"} in props["Tags"]
    bedrock_template.has_output("BedrockProfiles", Match.any_value())


def test_roles_may_invoke_only_through_the_tagged_profile(bedrock_template: Template) -> None:
    policies = json.dumps(bedrock_template.find_resources("AWS::IAM::Policy"))
    assert policies.count("bedrock:InvokeModel") >= 2  # job role and operator role
    assert "foundation-model/anthropic.claude-sonnet-4-6" in policies
    assert "InferenceProfileArn" in policies
    assert f"inference-profile/{SONNET}" not in policies  # the untagged system profile


def test_jobs_get_the_profile_mapping(bedrock_template: Template) -> None:
    definition = json.dumps(bedrock_template.find_resources("AWS::Batch::JobDefinition"))
    assert '"Name": "LIP_BEDROCK_PROFILES"' in definition


def test_budget_stop_blocks_model_calls_from_both_roles(bedrock_template: Template) -> None:
    bedrock_template.has_resource_properties(
        "AWS::IAM::ManagedPolicy",
        {
            "PolicyDocument": Match.object_like(
                {
                    "Statement": [
                        Match.object_like(
                            {
                                "Effect": "Deny",
                                "Action": Match.array_with(["batch:SubmitJob", "bedrock:InvokeModel"]),
                            }
                        )
                    ]
                }
            )
        },
    )
    action = bedrock_template.find_resources("AWS::Budgets::BudgetsAction")
    [props] = [r["Properties"] for r in action.values()]
    assert len(props["Definition"]["IamActionDefinition"]["Roles"]) == 2


def test_registry_bedrock_models_are_read_from_models_yaml() -> None:
    registry = Path(__file__).resolve().parents[2] / "src" / "lost_in_place" / "llm" / "models.yaml"
    assert SONNET in bedrock_models_from_registry(registry)
