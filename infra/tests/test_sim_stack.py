import json
import re
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Annotations, Match, Template

from lost_in_place_infra.sim_stack import (
    FROZEN_IMAGE_TAG_PREFIX,
    GITHUB_OIDC_HOST,
    SimSettings,
    SimStack,
    bedrock_models_from_registry,
    bedrock_regions_from_registry,
    imported_models_from_registry,
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
        f"{family}.{size}" for family in ("c7i", "m7i", "c6i") for size in ("xlarge", "2xlarge")
    )


def test_job_retries_spot_reclaims_and_times_out(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Batch::JobDefinition",
        {
            "RetryStrategy": Match.object_like({"Attempts": 3}),
            "Timeout": {"AttemptDurationSeconds": 5400},
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


GPT = "us.openai.gpt-6.1-sol"


@pytest.fixture(scope="module")
def two_model_template() -> Template:
    app = cdk.App()
    stack = SimStack(
        app,
        "WithGpt",
        settings=SimSettings(bedrock_models=(SONNET, GPT)),
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    return Template.from_stack(stack)


def bedrock_statements(template: Template) -> list[dict[str, object]]:
    return [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if "bedrock:InvokeModel" in json.dumps(statement["Action"])
    ]


def test_openai_model_gets_its_own_tagged_profile(two_model_template: Template) -> None:
    profiles = two_model_template.find_resources("AWS::Bedrock::ApplicationInferenceProfile")
    # Adding a model must not rename (and so replace) the profile an existing arm already runs on.
    assert sorted(profiles) == ["Bedrockanthropicclaudesonnet46", "Bedrockopenaigpt61sol"]
    props = profiles["Bedrockopenaigpt61sol"]["Properties"]
    assert props["InferenceProfileName"] == "WithGpt-openai-gpt-6-1-sol"
    assert json.dumps(props["ModelSource"]["CopyFrom"]).endswith(f':inference-profile/{GPT}"]]}}')
    assert {"Key": "project", "Value": "lost-in-place"} in props["Tags"]


def test_openai_foundation_model_is_granted_only_through_the_tagged_profiles(
    two_model_template: Template,
) -> None:
    statements = bedrock_statements(two_model_template)
    assert len(statements) == 4  # job and operator role: profiles, then foundation models
    policies = json.dumps(statements)
    assert f"inference-profile/{GPT}" not in policies
    assert "global.openai" not in policies
    for statement in statements:
        resources = json.dumps(statement["Resource"])
        if "foundation-model/" in resources:
            assert "foundation-model/openai.gpt-6.1-sol" in resources
            assert "foundation-model/anthropic.claude-sonnet-4-6" in resources
            condition = statement["Condition"]["StringEquals"]["bedrock:InferenceProfileArn"]
            assert {"Fn::GetAtt": ["Bedrockopenaigpt61sol", "InferenceProfileArn"]} in condition
        else:
            assert {"Fn::GetAtt": ["Bedrockopenaigpt61sol", "InferenceProfileArn"]} in statement["Resource"]


def test_no_marketplace_permissions_for_the_stack_roles(two_model_template: Template) -> None:
    # The model is enabled once in the console; the job and operator roles never subscribe.
    assert "aws-marketplace" not in json.dumps(two_model_template.find_resources("AWS::IAM::Policy"))


def test_jobs_map_the_openai_profile_too(two_model_template: Template) -> None:
    jobs = two_model_template.find_resources("AWS::Batch::JobDefinition").values()
    assert len(jobs) == 2
    for job in jobs:
        [env] = job["Properties"]["ContainerProperties"]["Environment"]
        mapping = json.dumps(env["Value"])
        assert env["Name"] == "LIP_BEDROCK_PROFILES"
        assert f'\\"{GPT}\\":\\"' in mapping
        assert "Bedrockopenaigpt61sol" in mapping


def test_budget_stop_covers_the_openai_profile(two_model_template: Template) -> None:
    [stop] = two_model_template.find_resources("AWS::IAM::ManagedPolicy").values()
    [deny] = stop["Properties"]["PolicyDocument"]["Statement"]
    assert deny["Effect"] == "Deny"
    assert deny["Resource"] == "*"
    assert {"bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"} <= set(deny["Action"])


def test_registry_bedrock_models_are_read_from_models_yaml() -> None:
    registry = Path(__file__).resolve().parents[2] / "src" / "lost_in_place" / "llm" / "models.yaml"
    assert SONNET in bedrock_models_from_registry(registry)
    assert "lip-qwen3-4b-thinking-2507-768f209" in imported_models_from_registry(registry)
    assert "lip-qwen3-4b-c1-c6bc0a6" in imported_models_from_registry(registry)
    assert SONNET not in imported_models_from_registry(registry)


SONNET_5_5 = "us.anthropic.claude-sonnet-5-5"
HAIKU_5_5 = "us.anthropic.claude-haiku-5-5"
EAST_2_PROFILES = {
    SONNET_5_5: "arn:aws:bedrock:us-east-2:123456789012:application-inference-profile/s55profile01",
    HAIKU_5_5: "arn:aws:bedrock:us-east-2:123456789012:application-inference-profile/h55profile01",
}


def east_2_stack(name: str, **overrides: Any) -> SimStack:
    settings: dict[str, Any] = {
        "bedrock_models": (HAIKU_5_5, SONNET, SONNET_5_5),
        "bedrock_regions": {SONNET: "us-east-1", SONNET_5_5: "us-east-2", HAIKU_5_5: "us-east-2"},
        "external_profiles": EAST_2_PROFILES,
    }
    return SimStack(
        cdk.App(),
        name,
        settings=SimSettings(**{**settings, **overrides}),
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )


@pytest.fixture(scope="module")
def east_2_template() -> Template:
    return Template.from_stack(east_2_stack("WithEast2"))


def test_no_us_east_1_profile_is_created_for_a_us_east_2_entry(east_2_template: Template) -> None:
    profiles = east_2_template.find_resources("AWS::Bedrock::ApplicationInferenceProfile")
    assert sorted(profiles) == ["Bedrockanthropicclaudesonnet46"]
    template = json.dumps(east_2_template.to_json())
    for model in (SONNET_5_5, HAIKU_5_5):
        assert f"inference-profile/{model}" not in template


def test_both_roles_invoke_the_us_east_2_profiles(east_2_template: Template) -> None:
    for role in ("JobRole", "OperatorRole"):
        [statement] = [
            s
            for s in granting(role_statements(east_2_template, role), "bedrock:InvokeModel")
            if "Condition" not in s and "imported-model" not in json.dumps(s["Resource"])
        ]
        assert statement["Action"] == ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        assert all(arn in statement["Resource"] for arn in EAST_2_PROFILES.values())
        assert {"Fn::GetAtt": ["Bedrockanthropicclaudesonnet46", "InferenceProfileArn"]} in statement[
            "Resource"
        ]


def test_us_east_2_foundation_models_only_under_the_profile_condition(east_2_template: Template) -> None:
    for role in ("JobRole", "OperatorRole"):
        statements = granting(role_statements(east_2_template, role), "bedrock:InvokeModel")
        [statement] = [s for s in statements if "foundation-model/" in json.dumps(s["Resource"])]
        resources = json.dumps(statement["Resource"])
        assert "bedrock:*::foundation-model/anthropic.claude-sonnet-5-5*" in resources
        assert "bedrock:*::foundation-model/anthropic.claude-haiku-5-5*" in resources
        condition = statement["Condition"]["StringEquals"]["bedrock:InferenceProfileArn"]
        assert all(arn in condition for arn in EAST_2_PROFILES.values())


def test_no_role_may_call_a_system_profile(east_2_template: Template) -> None:
    policies = json.dumps(east_2_template.find_resources("AWS::IAM::Policy"))
    assert re.search(r"(?<!application-)inference-profile/", policies) is None


def test_jobs_and_output_map_the_us_east_2_profiles(east_2_template: Template) -> None:
    jobs = east_2_template.find_resources("AWS::Batch::JobDefinition").values()
    [output] = east_2_template.find_outputs("BedrockProfiles").values()
    for mapping in [
        *(json.dumps(job["Properties"]["ContainerProperties"]["Environment"]) for job in jobs),
        json.dumps(output["Value"]),
    ]:
        for model, arn in EAST_2_PROFILES.items():
            assert f'\\"{model}\\":\\"{arn}\\"' in mapping


def test_budget_stop_covers_us_east_2_calls(east_2_template: Template) -> None:
    [stop] = east_2_template.find_resources("AWS::IAM::ManagedPolicy").values()
    [deny] = stop["Properties"]["PolicyDocument"]["Statement"]
    # Unconditioned on `*`, so it denies every region's endpoint; the budget's tag filter is
    # account-wide, so the us-east-2 profiles' project tag counts their spend.
    assert deny["Effect"] == "Deny"
    assert deny["Resource"] == "*"
    assert "Condition" not in deny
    assert {"bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"} <= set(deny["Action"])


def test_us_east_2_entry_without_a_profile_arn_is_not_callable() -> None:
    stack = east_2_stack("WithoutEast2Arns", external_profiles={})
    Annotations.from_stack(stack).has_warning(
        "*", Match.string_like_regexp(f"No profile ARN for {SONNET_5_5} \\(us-east-2\\)")
    )
    template = Template.from_stack(stack)
    assert "claude-sonnet-5-5" not in json.dumps(template.to_json())
    assert sorted(template.find_resources("AWS::Bedrock::ApplicationInferenceProfile")) == [
        "Bedrockanthropicclaudesonnet46"
    ]


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws:bedrock:us-east-2:123456789012:inference-profile/us.anthropic.claude-sonnet-5-5",
        "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/s55profile01",
        "arn:aws:bedrock:us-east-2:210987654321:application-inference-profile/s55profile01",
        "arn:aws:bedrock:us-east-2:123456789012:application-inference-profile/*",
    ],
)
def test_external_profile_must_be_this_accounts_tagged_profile_in_the_entry_region(arn: str) -> None:
    with pytest.raises(ValueError, match="expected an application inference profile"):
        east_2_stack("BadArn", external_profiles={**EAST_2_PROFILES, SONNET_5_5: arn})


def test_external_profile_for_an_in_region_model_is_rejected() -> None:
    arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/s46profile01"
    with pytest.raises(ValueError, match="not registered in another region"):
        east_2_stack("StrayArn", external_profiles={**EAST_2_PROFILES, SONNET: arn})


def test_registry_regions_default_to_us_east_1_and_are_one_per_model(tmp_path: Path) -> None:
    registry = tmp_path / "models.yaml"
    registry.write_text(
        f"a: {{provider: bedrock, model_id: {SONNET}}}\n"
        f"b: {{provider: bedrock, model_id: {SONNET_5_5}, region: us-east-2}}\n"
        f"b-fb1: {{provider: bedrock, model_id: {SONNET_5_5}, region: us-east-2, temperature: 0}}\n"
    )
    assert bedrock_regions_from_registry(registry) == {SONNET: "us-east-1", SONNET_5_5: "us-east-2"}
    registry.write_text(
        registry.read_text() + f"c: {{provider: bedrock, model_id: {SONNET_5_5}, region: us-west-2}}\n"
    )
    with pytest.raises(ValueError, match="registered in both us-east-2 and us-west-2"):
        bedrock_regions_from_registry(registry)


def test_registry_entries_of_other_regions_get_no_profile_in_the_stack_region() -> None:
    registry = Path(__file__).resolve().parents[2] / "src" / "lost_in_place" / "llm" / "models.yaml"
    models, regions = bedrock_models_from_registry(registry), bedrock_regions_from_registry(registry)
    others = {m: r for m, r in regions.items() if r != "us-east-1"}
    template = Template.from_stack(
        SimStack(
            cdk.App(),
            "FromRegistry",
            settings=SimSettings(
                bedrock_models=models,
                bedrock_regions=regions,
                external_profiles={
                    m: f"arn:aws:bedrock:{r}:123456789012:application-inference-profile/p{i:011d}"
                    for i, (m, r) in enumerate(others.items())
                },
            ),
            env=cdk.Environment(account="123456789012", region="us-east-1"),
        )
    )
    profiles = template.find_resources("AWS::Bedrock::ApplicationInferenceProfile").values()
    copied = [json.dumps(p["Properties"]["ModelSource"]["CopyFrom"]) for p in profiles]
    assert len(copied) == len(models) - len(others)
    for model in others:
        assert not any(c.endswith(f':inference-profile/{model}"]]}}') for c in copied)


def imported_statements(template: Template) -> list[dict[str, object]]:
    return [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if "imported-model" in json.dumps(statement.get("Resource"))
        and "bedrock:InvokeModel" in json.dumps(statement["Action"])
    ]


def test_roles_may_call_imported_models_but_not_import_them() -> None:
    app = cdk.App()
    stack = SimStack(
        app,
        "WithImported",
        settings=SimSettings(imported_models=("lip-qwen-test",)),
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    statements = imported_statements(Template.from_stack(stack))
    assert len(statements) == 2  # job role and operator role
    for statement in statements:
        assert statement["Action"] == [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
            "bedrock:GetImportedModel",
        ]
        assert json.dumps(statement["Resource"]).endswith(':imported-model/*"]]}')


def test_no_imported_model_grants_without_imported_models(template: Template) -> None:
    assert imported_statements(template) == []


def test_job_definition_can_pin_an_image_digest() -> None:
    digest = "sha256:" + "a" * 64
    app = cdk.App()
    stack = SimStack(
        app,
        "Pinned",
        settings=SimSettings(image_tag=digest),
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    jobs = Template.from_stack(stack).find_resources("AWS::Batch::JobDefinition").values()
    assert len(jobs) == 2
    for job in jobs:
        assert f"@{digest}" in json.dumps(job["Properties"]["ContainerProperties"]["Image"])


def role_statements(template: Template, role_prefix: str) -> list[dict[str, Any]]:
    return [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        if any(r["Ref"].startswith(role_prefix) for r in policy["Properties"]["Roles"])
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


def granting(statements: list[dict[str, Any]], action: str) -> list[dict[str, Any]]:
    def actions(s: dict[str, Any]) -> list[str]:
        return s["Action"] if isinstance(s["Action"], list) else [s["Action"]]

    return [s for s in statements if s["Effect"] == "Allow" and action in actions(s)]


def test_operator_reads_metrics_which_have_no_resource_scope(template: Template) -> None:
    operator = role_statements(template, "OperatorRole")
    for action in ("cloudwatch:GetMetricData", "cloudwatch:ListMetrics", "bedrock:ListImportedModels"):
        [statement] = granting(operator, action)
        assert statement["Resource"] == "*"
        assert "Condition" not in statement


def test_operator_reads_only_this_projects_budget(template: Template) -> None:
    operator = role_statements(template, "OperatorRole")
    for action in ("budgets:ViewBudget", "budgets:DescribeBudgetActionsForBudget"):
        [statement] = granting(operator, action)
        assert json.dumps(statement["Resource"]).endswith(':budgets::123456789012:budget/Test-monthly"]]}')


def test_operator_lists_policies_of_the_two_stopped_roles_only(template: Template) -> None:
    [statement] = granting(role_statements(template, "OperatorRole"), "iam:ListAttachedRolePolicies")
    roles = sorted(r["Fn::GetAtt"][0] for r in statement["Resource"])
    assert len(roles) == 2
    assert roles[0].startswith("JobRole")
    assert roles[1].startswith("OperatorRole")


def test_operator_deletes_only_project_tagged_imports(template: Template) -> None:
    assert granting(role_statements(template, "OperatorRole"), "bedrock:DeleteImportedModel") == []
    app = cdk.App()
    stack = SimStack(
        app,
        "WithImportedDelete",
        settings=SimSettings(imported_models=("lip-qwen-test",)),
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    imported = Template.from_stack(stack)
    [statement] = granting(role_statements(imported, "OperatorRole"), "bedrock:DeleteImportedModel")
    assert json.dumps(statement["Resource"]).endswith(':imported-model/*"]]}')
    assert statement["Condition"] == {"StringEquals": {"aws:ResourceTag/project": "lost-in-place"}}
    assert granting(role_statements(imported, "JobRole"), "bedrock:DeleteImportedModel") == []


def test_operator_describes_this_stack_only(template: Template) -> None:
    operator = role_statements(template, "OperatorRole")
    for action in ("cloudformation:DescribeStacks", "cloudformation:DescribeStackResources"):
        [statement] = granting(operator, action)
        assert json.dumps(statement["Resource"]).endswith(':stack/Test/*"]]}')


def test_submitter_grants_stay_off_the_job_role(template: Template) -> None:
    policies = json.dumps(role_statements(template, "JobRole"))
    for prefix in ("cloudwatch:", "budgets:", "iam:", "cloudformation:", "bedrock:DeleteImportedModel"):
        assert prefix not in policies


def test_only_the_operator_may_submit_jobs(template: Template) -> None:
    submitters = [
        role
        for policy in template.find_resources("AWS::IAM::Policy").values()
        if granting(policy["Properties"]["PolicyDocument"]["Statement"], "batch:SubmitJob")
        for role in policy["Properties"]["Roles"]
    ]
    assert len(submitters) == 1
    assert submitters[0]["Ref"].startswith("OperatorRole")


def test_frozen_images_outlive_the_keep_ten_rule(template: Template) -> None:
    [repo] = template.find_resources("AWS::ECR::Repository").values()
    rules = json.loads(repo["Properties"]["LifecyclePolicy"]["LifecyclePolicyText"])["rules"]
    assert [(r["rulePriority"], r["selection"]) for r in rules] == [
        (
            1,
            {
                "tagStatus": "tagged",
                "tagPrefixList": [FROZEN_IMAGE_TAG_PREFIX],
                "countType": "imageCountMoreThan",
                "countNumber": 100,
            },
        ),
        (2, {"tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": 10}),
    ]


def test_sweep_jobs_run_one_manifest_entry_each(template: Template) -> None:
    jobs = template.find_resources("AWS::Batch::JobDefinition")
    commands = [j["Properties"]["ContainerProperties"]["Command"] for j in jobs.values()]
    assert [
        "run-manifest",
        "Ref::manifest",
        "--offset",
        "Ref::offset",
        "--analyze",
        "--upload",
        "Ref::upload",
    ] in commands
    assert all(j["Properties"]["RetryStrategy"]["Attempts"] == 3 for j in jobs.values())


def test_jobs_read_manifests_and_past_attempts_and_the_operator_writes_manifests(template: Template) -> None:
    policies = json.dumps(template.find_resources("AWS::IAM::Policy"))
    assert "/manifests/*" in policies
    assert "/runs/*" in policies
