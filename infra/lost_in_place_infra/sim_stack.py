"""Spot AWS Batch for headless PX4 + Gazebo episodes, with a hard spending ceiling.

Cost controls, strongest first:
1. `max_vcpus` on the compute environment caps how much can run at once.
2. An AWS Budgets action attaches a deny policy to the operator and job roles at 100% of the
   budget, so no new jobs can be submitted and no more planner model calls can be made.
3. Budget emails at 50/80/100% actual and 100% forecast.

The budget counts only costs tagged `project=lost-in-place`, so other workloads in a shared account
neither trip the stop nor hide this project's spend. That needs `project` activated as a cost
allocation tag in the Billing console; until then the budget sees $0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from aws_cdk import (
    Annotations,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Size,
    Stack,
    Tags,
)
from aws_cdk import aws_batch as batch
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from constructs import Construct

# x86 only: the Gazebo flow plugin's quality cast is undefined behaviour and may differ on ARM.
# Intel only: on fresh c7a (AMD) hosts 4 of 8 first episodes failed or never took off, against
# 0 of 16 on m7i and c7i (docs/fidelity.md).
INSTANCE_CLASSES = [
    ec2.InstanceClass.C7I,
    ec2.InstanceClass.M7I,
    ec2.InstanceClass.C6I,
]
# No 2-vCPU sizes: the first episode on a fresh .large failed 4 of 9 times (docs/fidelity.md).
# Sizes above 2xlarge would start more packed episodes at once on a fresh host, which is untested.
INSTANCE_SIZES = [ec2.InstanceSize.XLARGE, ec2.InstanceSize.XLARGE2]
INSTANCE_TYPES = [ec2.InstanceType.of(c, s) for c in INSTANCE_CLASSES for s in INSTANCE_SIZES]
EPISODE_VCPUS = 2  # two episodes per 4 vCPUs was reliable in testing; three was not
EPISODE_MEMORY_MIB = 3072
# A thinking model can spend about a minute on each of a mission's 40 planner calls.
EPISODE_TIMEOUT = Duration.minutes(90)

PROJECT_TAG_KEY = "project"
PROJECT_TAG_VALUE = "lost-in-place"
GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"
BEDROCK_PROFILES_ENV = "LIP_BEDROCK_PROFILES"  # read by lost_in_place.llm.clients
FROZEN_IMAGE_TAG_PREFIX = "freeze-"


@dataclass(frozen=True)
class SimSettings:
    budget_usd: float = 100.0
    max_vcpus: int = 128
    alert_email: str = ""
    image_tag: str = "latest"
    # Account-wide EstimatedCharges alarm threshold; 0 disables it. Billing metrics cannot be
    # filtered by tag, so this alarm also counts every other workload in the account.
    account_alarm_usd: float = 0.0
    # Repository segment of the GitHub OIDC `sub` claim whose main branch may push images; empty
    # disables it. Repositories with immutable subjects (every repo created after 2026-07-15) use
    # "owner@OWNER_ID/name@REPO_ID", not "owner/name"; GitHub's
    # GET /repos/{owner}/{repo}/actions/oidc/customization/sub returns it as sub_claim_prefix.
    # Needs the account's token.actions.githubusercontent.com OIDC provider, which is shared by
    # every stack in the account and so is imported rather than created here.
    github_repo: str = ""
    # Bedrock system inference profiles the planner may call (e.g. "us.anthropic.claude-sonnet-4-6").
    # Each gets a tagged application inference profile, so its spend counts against the budget.
    bedrock_models: tuple[str, ...] = ()
    # Names of models imported with Bedrock Custom Model Import that the planner may call.
    imported_models: tuple[str, ...] = ()


def _registry_ids(path: Path, provider: str) -> tuple[str, ...]:
    entries = yaml.safe_load(path.read_text()) or {}
    return tuple(sorted({e["model_id"] for e in entries.values() if e.get("provider") == provider}))


def bedrock_models_from_registry(path: Path) -> tuple[str, ...]:
    """The Bedrock model ids in the planner registry (`src/lost_in_place/llm/models.yaml`)."""
    return _registry_ids(path, "bedrock")


def imported_models_from_registry(path: Path) -> tuple[str, ...]:
    """The imported-model names in the planner registry."""
    return _registry_ids(path, "bedrock_imported")


class SimStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, settings: SimSettings, **kwargs: object
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]
        # The compute environment forwards this to the spot instances it launches.
        Tags.of(self).add(PROJECT_TAG_KEY, PROJECT_TAG_VALUE)

        alerts = sns.Topic(self, "Alerts", display_name="lost-in-place cost alerts")
        alerts.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[iam.ServicePrincipal("budgets.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[alerts.topic_arn],
            )
        )
        if settings.alert_email:
            alerts.add_subscription(subs.EmailSubscription(settings.alert_email))
        else:
            Annotations.of(self).add_warning("No alertEmail context set: budget alerts have no subscriber.")

        results = s3.Bucket(
            self,
            "Results",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    prefix="runs/",
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                            transition_after=Duration.days(30),
                        )
                    ],
                )
            ],
        )

        images = ecr.Repository(
            self,
            "Images",
            image_scan_on_push=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                # A frozen design's record names its image by digest; a lower-priority rule can't
                # expire an image this rule matches, so retagging it freeze-* keeps it pullable.
                ecr.LifecycleRule(
                    rule_priority=1,
                    tag_status=ecr.TagStatus.TAGGED,
                    tag_prefix_list=[FROZEN_IMAGE_TAG_PREFIX],
                    max_image_count=100,
                ),
                ecr.LifecycleRule(rule_priority=2, max_image_count=10),
            ],
        )

        # Public subnets and no NAT gateway: instances pull images and write results directly.
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=3,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=20)
            ],
            gateway_endpoints={
                "S3": ec2.GatewayVpcEndpointOptions(service=ec2.GatewayVpcEndpointAwsService.S3)
            },
        )

        compute = batch.ManagedEc2EcsComputeEnvironment(
            self,
            "SpotCompute",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            spot=True,
            allocation_strategy=batch.AllocationStrategy.SPOT_PRICE_CAPACITY_OPTIMIZED,
            instance_types=INSTANCE_TYPES,
            use_optimal_instance_classes=False,
            minv_cpus=0,
            maxv_cpus=settings.max_vcpus,
        )
        queue = batch.JobQueue(self, "Queue", priority=1)
        queue.add_compute_environment(compute, 1)

        job_role = iam.Role(self, "JobRole", assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
        results.grant_put(job_role, "runs/*")
        log_group = logs.LogGroup(self, "JobLogs", retention=logs.RetentionDays.ONE_MONTH)

        # Planner calls go through tagged application inference profiles; the system profiles they
        # copy are not granted, so untagged (unbudgeted) model spend isn't possible from these roles.
        bedrock_profiles: dict[str, str] = {}
        foundation_models: list[str] = []
        for model in settings.bedrock_models:
            foundation_model = model.split(".", 1)[1]  # "us.anthropic.x" -> "anthropic.x"
            profile = bedrock.CfnApplicationInferenceProfile(
                self,
                f"Bedrock-{foundation_model}",
                inference_profile_name=f"{self.stack_name}-{re.sub('[^0-9a-zA-Z]+', '-', foundation_model)}",
                description=f"lost-in-place planner calls to {model}",
                model_source=bedrock.CfnApplicationInferenceProfile.InferenceProfileModelSourceProperty(
                    copy_from=f"arn:{self.partition}:bedrock:{self.region}:{self.account}:inference-profile/{model}"
                ),
            )
            bedrock_profiles[model] = profile.attr_inference_profile_arn
            foundation_models.append(f"arn:{self.partition}:bedrock:*::foundation-model/{foundation_model}")
        bedrock_invoke = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]

        def grant_bedrock(role: iam.Role) -> None:
            if settings.imported_models:
                # An imported model's ARN ends in an id that changes with every import, so this
                # covers every model imported into the account and region; imports aren't granted.
                role.add_to_policy(
                    iam.PolicyStatement(
                        actions=[*bedrock_invoke, "bedrock:GetImportedModel"],
                        resources=[
                            f"arn:{self.partition}:bedrock:{self.region}:{self.account}:imported-model/*"
                        ],
                    )
                )
            if not bedrock_profiles:
                return
            profiles = list(bedrock_profiles.values())
            role.add_to_policy(iam.PolicyStatement(actions=bedrock_invoke, resources=profiles))
            # A profile routes to the model in several regions; allow that only through our profiles.
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=bedrock_invoke,
                    resources=foundation_models,
                    conditions={"StringEquals": {"bedrock:InferenceProfileArn": profiles}},
                )
            )

        grant_bedrock(job_role)

        def episode_job(name: str, command: list[str], parameters: dict[str, str]) -> batch.EcsJobDefinition:
            return batch.EcsJobDefinition(
                self,
                name,
                container=batch.EcsEc2ContainerDefinition(
                    self,
                    f"{name}Container",
                    image=ecs.ContainerImage.from_ecr_repository(images, settings.image_tag),
                    cpu=EPISODE_VCPUS,
                    memory=Size.mebibytes(EPISODE_MEMORY_MIB),
                    job_role=job_role,
                    logging=ecs.LogDriver.aws_logs(stream_prefix="episode", log_group=log_group),
                    environment={BEDROCK_PROFILES_ENV: self.to_json_string(bedrock_profiles)}
                    if bedrock_profiles
                    else None,
                    command=command,
                ),
                parameters=parameters,
                retry_attempts=3,
                retry_strategies=[
                    batch.RetryStrategy.of(batch.Action.RETRY, batch.Reason.SPOT_INSTANCE_RECLAIMED)
                ],
                timeout=EPISODE_TIMEOUT,
            )

        runs_uri = f"s3://{results.bucket_name}/runs"
        job = episode_job(
            "Episode",
            ["run", "Ref::scenario", "--seed", "Ref::seed", "--analyze", "--upload", "Ref::upload"],
            {"scenario": "scenarios/fixtures/textured_hover.yaml", "seed": "0", "upload": runs_uri},
        )
        # One manifest entry per array child (`lip manifest`); skips episodes that already have a result.
        sweep_job = episode_job(
            "SweepEpisode",
            [
                "run-manifest",
                "Ref::manifest",
                "--offset",
                "Ref::offset",
                "--analyze",
                "--upload",
                "Ref::upload",
            ],
            {
                "manifest": f"s3://{results.bucket_name}/manifests/sweep.json",
                "offset": "0",
                "upload": runs_uri,
            },
        )
        results.grant_read(job_role, "manifests/*")
        results.grant_read(job_role, "runs/*")  # to see which episodes already have a result

        # The role used to submit jobs and read results. The budget action blocks it at 100%.
        operator = iam.Role(
            self,
            "OperatorRole",
            assumed_by=iam.AccountRootPrincipal(),
            description="Submit lost-in-place episodes and read their results",
            max_session_duration=Duration.hours(12),
        )
        operator.add_to_policy(
            iam.PolicyStatement(
                actions=["batch:SubmitJob"],
                resources=[
                    queue.job_queue_arn,
                    f"arn:{self.partition}:batch:{self.region}:{self.account}:job-definition/*",
                ],
            )
        )
        operator.add_to_policy(
            iam.PolicyStatement(
                actions=["batch:DescribeJobs", "batch:ListJobs", "batch:TerminateJob", "batch:CancelJob"],
                resources=["*"],
            )
        )
        results.grant_read(operator)
        results.grant_put(operator, "manifests/*")
        images.grant_pull_push(operator)
        # For `lip propose` and planner checks outside Batch; the budget stop covers this too.
        grant_bedrock(operator)

        outputs: dict[str, str] = {}
        if settings.github_repo:
            github_oidc = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
                self,
                "GitHubOidc",
                f"arn:{self.partition}:iam::{self.account}:oidc-provider/{GITHUB_OIDC_HOST}",
            )
            image_push = iam.Role(
                self,
                "ImagePushRole",
                assumed_by=iam.WebIdentityPrincipal(
                    github_oidc.open_id_connect_provider_arn,
                    conditions={
                        "StringEquals": {
                            f"{GITHUB_OIDC_HOST}:aud": "sts.amazonaws.com",
                            f"{GITHUB_OIDC_HOST}:sub": f"repo:{settings.github_repo}:ref:refs/heads/main",
                        }
                    },
                ),
                description=f"Push lost-in-place images from {settings.github_repo} main",
                max_session_duration=Duration.hours(1),
            )
            images.grant_pull_push(image_push)
            outputs["ImagePushRoleArn"] = image_push.role_arn

        deny_compute = iam.ManagedPolicy(
            self,
            "BudgetStop",
            description="Attached by AWS Budgets when lost-in-place spending reaches the budget",
            statements=[
                iam.PolicyStatement(
                    effect=iam.Effect.DENY, actions=["batch:SubmitJob", *bedrock_invoke], resources=["*"]
                )
            ],
        )
        budget_executor = iam.Role(
            self,
            "BudgetActionRole",
            assumed_by=iam.ServicePrincipal("budgets.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AWSBudgetsActionsWithAWSResourceControlAccess"
                )
            ],
        )

        budget_name = f"{self.stack_name}-monthly"
        budget = budgets.CfnBudget(
            self,
            "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=budget_name,
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=settings.budget_usd, unit="USD"),
                cost_filters={"TagKeyValue": [f"user:{PROJECT_TAG_KEY}${PROJECT_TAG_VALUE}"]},
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type=kind,
                        threshold=pct,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            address=alerts.topic_arn, subscription_type="SNS"
                        )
                    ],
                )
                for kind, pct in (("ACTUAL", 50), ("ACTUAL", 80), ("ACTUAL", 100), ("FORECASTED", 100))
            ],
        )
        stop_action = budgets.CfnBudgetsAction(
            self,
            "StopAtBudget",
            budget_name=budget_name,
            action_type="APPLY_IAM_POLICY",
            approval_model="AUTOMATIC",
            notification_type="ACTUAL",
            action_threshold=budgets.CfnBudgetsAction.ActionThresholdProperty(type="PERCENTAGE", value=100),
            definition=budgets.CfnBudgetsAction.DefinitionProperty(
                iam_action_definition=budgets.CfnBudgetsAction.IamActionDefinitionProperty(
                    policy_arn=deny_compute.managed_policy_arn, roles=[operator.role_name, job_role.role_name]
                )
            ),
            execution_role_arn=budget_executor.role_arn,
            subscribers=[budgets.CfnBudgetsAction.SubscriberProperty(type="SNS", address=alerts.topic_arn)],
        )
        stop_action.node.add_dependency(budget)

        if settings.account_alarm_usd > 0:
            # Billing metrics exist only in us-east-1 and need "Receive Billing Alerts" enabled.
            billing = cloudwatch.Alarm(
                self,
                "AccountBillingAlarm",
                metric=cloudwatch.Metric(
                    namespace="AWS/Billing",
                    metric_name="EstimatedCharges",
                    dimensions_map={"Currency": "USD"},
                    statistic="Maximum",
                    period=Duration.hours(6),
                ),
                threshold=settings.account_alarm_usd,
                evaluation_periods=1,
                alarm_description="Estimated charges for the whole AWS account (all projects) passed "
                f"{settings.account_alarm_usd:g} USD",
            )
            billing.add_alarm_action(cw_actions.SnsAction(alerts))

        for name, value in {
            "ResultsBucket": results.bucket_name,
            "ImageRepository": images.repository_uri,
            "JobQueue": queue.job_queue_name,
            "JobDefinition": job.job_definition_name,
            "SweepJobDefinition": sweep_job.job_definition_name,
            "OperatorRoleArn": operator.role_arn,
            **outputs,
        }.items():
            CfnOutput(self, name, value=value)
        if bedrock_profiles:
            CfnOutput(
                self,
                "BedrockProfiles",
                value=self.to_json_string(bedrock_profiles),
                description=f"Export as {BEDROCK_PROFILES_ENV} to call the planner models outside Batch",
            )
