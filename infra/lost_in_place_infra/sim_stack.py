"""Spot AWS Batch for headless PX4 + Gazebo episodes, with a hard spending ceiling.

Cost controls, strongest first:
1. `max_vcpus` on the compute environment caps how much can run at once.
2. An AWS Budgets action attaches a deny policy to the operator role at 100% of the budget,
   so no new jobs can be submitted through it.
3. Budget emails at 50/80/100% actual and 100% forecast.

The budget counts only costs tagged `project=lost-in-place`, so other workloads in a shared account
neither trip the stop nor hide this project's spend. That needs `project` activated as a cost
allocation tag in the Billing console; until then the budget sees $0.
"""

from __future__ import annotations

from dataclasses import dataclass

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
INSTANCE_CLASSES = [
    ec2.InstanceClass.C7I,
    ec2.InstanceClass.C7A,
    ec2.InstanceClass.M7I,
    ec2.InstanceClass.C6I,
]
EPISODE_VCPUS = 2  # two episodes per 4 vCPUs was reliable in testing; three was not
EPISODE_MEMORY_MIB = 3072
EPISODE_TIMEOUT = Duration.minutes(30)

PROJECT_TAG_KEY = "project"
PROJECT_TAG_VALUE = "lost-in-place"


@dataclass(frozen=True)
class SimSettings:
    budget_usd: float = 100.0
    max_vcpus: int = 32
    alert_email: str = ""
    image_tag: str = "latest"
    # Account-wide EstimatedCharges alarm threshold; 0 disables it. Billing metrics cannot be
    # filtered by tag, so this alarm also counts every other workload in the account.
    account_alarm_usd: float = 0.0


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
            lifecycle_rules=[ecr.LifecycleRule(max_image_count=10)],
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
            instance_classes=INSTANCE_CLASSES,
            use_optimal_instance_classes=False,
            minv_cpus=0,
            maxv_cpus=settings.max_vcpus,
        )
        queue = batch.JobQueue(self, "Queue", priority=1)
        queue.add_compute_environment(compute, 1)

        job_role = iam.Role(self, "JobRole", assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
        results.grant_put(job_role, "runs/*")
        log_group = logs.LogGroup(self, "JobLogs", retention=logs.RetentionDays.ONE_MONTH)

        job = batch.EcsJobDefinition(
            self,
            "Episode",
            container=batch.EcsEc2ContainerDefinition(
                self,
                "EpisodeContainer",
                image=ecs.ContainerImage.from_ecr_repository(images, settings.image_tag),
                cpu=EPISODE_VCPUS,
                memory=Size.mebibytes(EPISODE_MEMORY_MIB),
                job_role=job_role,
                logging=ecs.LogDriver.aws_logs(stream_prefix="episode", log_group=log_group),
                command=[
                    "run",
                    "Ref::scenario",
                    "--seed",
                    "Ref::seed",
                    "--analyze",
                    "--upload",
                    "Ref::upload",
                ],
            ),
            parameters={
                "scenario": "scenarios/fixtures/textured_hover.yaml",
                "seed": "0",
                "upload": f"s3://{results.bucket_name}/runs",
            },
            retry_attempts=3,
            retry_strategies=[
                batch.RetryStrategy.of(batch.Action.RETRY, batch.Reason.SPOT_INSTANCE_RECLAIMED)
            ],
            timeout=EPISODE_TIMEOUT,
        )

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
        images.grant_pull_push(operator)

        deny_compute = iam.ManagedPolicy(
            self,
            "BudgetStop",
            description="Attached by AWS Budgets when lost-in-place spending reaches the budget",
            statements=[
                iam.PolicyStatement(effect=iam.Effect.DENY, actions=["batch:SubmitJob"], resources=["*"])
            ],
        )
        budget_executor = iam.Role(
            self,
            "BudgetActionRole",
            assumed_by=iam.ServicePrincipal("budgets.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSBudgetsActionsWithAWSResourceControlAccess"
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
                    policy_arn=deny_compute.managed_policy_arn, roles=[operator.role_name]
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
            "OperatorRoleArn": operator.role_arn,
        }.items():
            CfnOutput(self, name, value=value)
