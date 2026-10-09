import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from lost_in_place_infra.sim_stack import SimSettings, SimStack


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
                    "InstanceTypes": ["c7i", "c7a", "m7i", "c6i"],
                }
            )
        },
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
                {"BudgetLimit": {"Amount": 50, "Unit": "USD"}, "TimeUnit": "MONTHLY"}
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
