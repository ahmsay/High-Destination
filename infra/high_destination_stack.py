"""High Destination: scale-to-zero GPU Spot worker for wallpaper generation.

Resources:
  - S3 bucket: model weights (models/), results (outputs/), worker logs (logs/)
  - SQS queue (+ DLQ): generation jobs
  - VPC with public subnets only (no NAT gateway) + free S3 gateway endpoint
  - Auto Scaling group, min 0 / max 1, Spot, GPU instances running ComfyUI.
    The CLI raises desired capacity to 1 when it submits a job; the worker
    terminates itself (and decrements capacity) after being idle.
  - Optional (context onDemandFallbackMinutes > 0): a Lambda that switches the
    group to On-Demand while Spot launches keep failing, see fallback/index.py.
  - Optional (context web=true): the web UI behind a Cognito login, see web.py.
"""
from pathlib import Path
from typing import Optional

import aws_cdk as cdk
from aws_cdk import (
    aws_autoscaling as autoscaling,
    aws_budgets as budgets,
    aws_certificatemanager as acm,
    aws_ec2 as ec2,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_route53 as route53,
    aws_s3 as s3,
    aws_s3_assets as s3_assets,
    aws_sqs as sqs,
)
from constructs import Construct

from web import WebApp

WORKER_DIR = Path(__file__).resolve().parent.parent / "worker"
FALLBACK_DIR = Path(__file__).resolve().parent / "fallback"
AMI_PARAM = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id"
WORKER_TAG = ("hd:role", "worker")


def zone_for(scope: Construct, name: Optional[str]) -> Optional[route53.IHostedZone]:
    """The Route 53 hosted zone of your domain (context hostedZone), if it is in this account."""
    return route53.HostedZone.from_lookup(scope, "Zone", domain_name=name) if name else None


class CertStack(cdk.Stack):
    """CloudFront only takes certificates from us-east-1, so the web domain's lives here."""

    def __init__(self, scope: Construct, construct_id: str, *, domain: str, hosted_zone: Optional[str],
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        # Without a hosted zone, ACM waits (and so does the deploy) until you add the
        # validation CNAME it shows in the ACM console to your DNS.
        self.certificate = acm.Certificate(self, "Cert", domain_name=domain,
                                           validation=acm.CertificateValidation.from_dns(zone_for(self, hosted_zone)))


class HighDestinationStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, *, web: bool = False, domain: Optional[str] = None,
                 hosted_zone: Optional[str] = None, certificate: Optional[acm.ICertificate] = None,
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        instance_types: list[str] = self.node.try_get_context("instanceTypes") or ["g6.xlarge"]
        idle_minutes = int(self.node.try_get_context("idleMinutes") or 10)
        comfyui_version = self.node.try_get_context("comfyuiVersion") or "master"
        budget_email = self.node.try_get_context("budgetEmail")
        fallback_minutes = int(self.node.try_get_context("onDemandFallbackMinutes") or 0)

        # ---- Storage -------------------------------------------------------
        bucket = s3.Bucket(
            self,
            "Bucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,  # keeps your wallpapers if the stack is deleted
            lifecycle_rules=[
                s3.LifecycleRule(prefix="logs/", expiration=cdk.Duration.days(14)),
                s3.LifecycleRule(abort_incomplete_multipart_upload_after=cdk.Duration.days(1)),
            ],
        )

        # ---- Queue ---------------------------------------------------------
        dlq = sqs.Queue(self, "JobsDlq", retention_period=cdk.Duration.days(14))
        queue = sqs.Queue(
            self,
            "Jobs",
            # Long enough for one job incl. 4x upscale; a Spot interruption
            # makes the message visible again after this timeout.
            visibility_timeout=cdk.Duration.minutes(15),
            retention_period=cdk.Duration.days(1),
            dead_letter_queue=sqs.DeadLetterQueue(queue=dlq, max_receive_count=3),
        )

        # ---- Network (no NAT) ---------------------------------------------
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=3,  # more AZs = better odds of Spot GPU capacity
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24)
            ],
            gateway_endpoints={"S3": ec2.GatewayVpcEndpointOptions(service=ec2.GatewayVpcEndpointAwsService.S3)},
        )
        sg = ec2.SecurityGroup(self, "WorkerSg", vpc=vpc, description="High Destination worker (no inbound)")

        # ---- Worker role ---------------------------------------------------
        role = iam.Role(
            self,
            "WorkerRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")],
        )
        bucket.grant_read_write(role)
        queue.grant_consume_messages(role)
        # Self-termination. Scoped by tag to avoid a role <-> ASG dependency cycle.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["autoscaling:TerminateInstanceInAutoScalingGroup"],
                resources=["*"],
                conditions={"StringEquals": {f"autoscaling:ResourceTag/{WORKER_TAG[0]}": WORKER_TAG[1]}},
            )
        )
        # Optional Hugging Face token for gated models (SecureString, created by you).
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[self.format_arn(service="ssm", resource="parameter", resource_name="high-destination/*")],
            )
        )

        worker_asset = s3_assets.Asset(self, "WorkerCode", path=str(WORKER_DIR))
        worker_asset.grant_read(role)

        # ---- Launch template ----------------------------------------------
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "set -euxo pipefail",
            "exec > >(tee -a /var/log/hd-bootstrap.log) 2>&1",
            f"export AWS_REGION={self.region} AWS_DEFAULT_REGION={self.region}",
            f"export HD_BUCKET={bucket.bucket_name}",
            f"export HD_QUEUE_URL={queue.queue_url}",
            f"export HD_IDLE_MINUTES={idle_minutes}",
            f"export HD_COMFYUI_VERSION={comfyui_version}",
            "command -v aws >/dev/null || snap install aws-cli --classic",
            "mkdir -p /opt/hd",
            f"aws s3 cp {worker_asset.s3_object_url} /tmp/worker.zip",
            "python3 -m zipfile -e /tmp/worker.zip /opt/hd",
            "bash /opt/hd/bootstrap.sh",
        )

        launch_template = ec2.LaunchTemplate(
            self,
            "WorkerLt",
            machine_image=ec2.MachineImage.from_ssm_parameter(AMI_PARAM, os=ec2.OperatingSystemType.LINUX),
            instance_type=ec2.InstanceType(instance_types[0]),
            role=role,
            security_group=sg,
            user_data=user_data,
            require_imdsv2=True,
            associate_public_ip_address=True,
            instance_initiated_shutdown_behavior=ec2.InstanceInitiatedShutdownBehavior.TERMINATE,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/sda1",
                    volume=ec2.BlockDeviceVolume.ebs(
                        100, volume_type=ec2.EbsDeviceVolumeType.GP3, delete_on_termination=True
                    ),
                )
            ],
        )

        # Spot comes from instances_distribution below, not from the launch template,
        # so the console's "Request Spot Instances: No" (a launch template field) is expected.
        asg = autoscaling.AutoScalingGroup(
            self,
            "WorkerAsg",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            min_capacity=0,
            max_capacity=1,
            # Backstop: no instance lives longer than a day, whatever happens.
            max_instance_lifetime=cdk.Duration.days(1),
            mixed_instances_policy=autoscaling.MixedInstancesPolicy(
                launch_template=launch_template,
                launch_template_overrides=[
                    autoscaling.LaunchTemplateOverrides(instance_type=ec2.InstanceType(t)) for t in instance_types
                ],
                instances_distribution=autoscaling.InstancesDistribution(
                    on_demand_base_capacity=0,
                    on_demand_percentage_above_base_capacity=0,
                    spot_allocation_strategy=autoscaling.SpotAllocationStrategy.PRICE_CAPACITY_OPTIMIZED,
                    # Only used by the On-Demand fallback: the cheapest type that has capacity.
                    on_demand_allocation_strategy=autoscaling.OnDemandAllocationStrategy.LOWEST_PRICE,
                ),
            ),
        )
        cdk.Tags.of(asg).add(*WORKER_TAG)
        cdk.Tags.of(asg).add("Name", "high-destination-worker")

        # ---- Optional On-Demand fallback ----------------------------------
        if fallback_minutes > 0:
            fallback = lambda_.Function(
                self,
                "SpotFallback",
                description="High Destination: switch the worker to On-Demand while Spot is unavailable",
                runtime=lambda_.Runtime.PYTHON_3_12,
                architecture=lambda_.Architecture.ARM_64,
                handler="index.handler",
                code=lambda_.Code.from_asset(str(FALLBACK_DIR)),
                timeout=cdk.Duration.seconds(30),
                environment={"HD_ASG": asg.auto_scaling_group_name, "HD_FALLBACK_MINUTES": str(fallback_minutes)},
                log_group=logs.LogGroup(self, "SpotFallbackLogs", retention=logs.RetentionDays.TWO_WEEKS,
                                        removal_policy=cdk.RemovalPolicy.DESTROY),
            )
            fallback.add_to_role_policy(iam.PolicyStatement(
                actions=["autoscaling:DescribeAutoScalingGroups", "autoscaling:DescribeScalingActivities"],
                resources=["*"],
            ))
            fallback.add_to_role_policy(iam.PolicyStatement(
                actions=["autoscaling:UpdateAutoScalingGroup"],
                resources=["*"],
                conditions={"StringEquals": {f"autoscaling:ResourceTag/{WORKER_TAG[0]}": WORKER_TAG[1]}},
            ))
            # Updating a group that uses a launch template checks that the caller could launch it.
            fallback.add_to_role_policy(iam.PolicyStatement(
                actions=["ec2:RunInstances", "ec2:CreateTags", "ec2:DescribeLaunchTemplateVersions"],
                resources=["*"],
            ))
            role.grant_pass_role(fallback)
            events.Rule(
                self,
                "WorkerLaunchEvents",
                event_pattern=events.EventPattern(
                    source=["aws.autoscaling"],
                    detail_type=["EC2 Instance Launch Unsuccessful", "EC2 Instance Terminate Successful"],
                    detail={"AutoScalingGroupName": [asg.auto_scaling_group_name]},
                ),
                targets=[targets.LambdaFunction(fallback, retry_attempts=2)],
            )

        # ---- Optional budget alarm ----------------------------------------
        if budget_email:
            budgets.CfnBudget(
                self,
                "Budget",
                budget=budgets.CfnBudget.BudgetDataProperty(
                    budget_type="COST",
                    time_unit="MONTHLY",
                    budget_limit=budgets.CfnBudget.SpendProperty(amount=10, unit="USD"),
                ),
                notifications_with_subscribers=[
                    budgets.CfnBudget.NotificationWithSubscribersProperty(
                        notification=budgets.CfnBudget.NotificationProperty(
                            comparison_operator="GREATER_THAN",
                            notification_type=kind,
                            threshold=threshold,
                            threshold_type="PERCENTAGE",
                        ),
                        subscribers=[
                            budgets.CfnBudget.SubscriberProperty(subscription_type="EMAIL", address=budget_email)
                        ],
                    )
                    for kind, threshold in (("ACTUAL", 80), ("FORECASTED", 100))
                ],
            )

        # ---- Web UI (optional) ---------------------------------------------
        if web:
            zone = zone_for(self, hosted_zone) if domain else None
            if domain and certificate is None:  # this stack is in us-east-1 itself
                certificate = acm.Certificate(self, "WebCert", domain_name=domain,
                                              validation=acm.CertificateValidation.from_dns(zone))
            WebApp(
                self,
                "Web",
                bucket=bucket,
                queue=queue,
                asg=asg,
                worker_tag=WORKER_TAG,
                domain=domain,
                certificate=certificate,
                zone=zone,
                auth_domain_prefix=self.node.try_get_context("authDomainPrefix"),
            )

        # ---- Outputs (read by the CLI) ------------------------------------
        cdk.CfnOutput(self, "BucketName", value=bucket.bucket_name)
        cdk.CfnOutput(self, "QueueUrl", value=queue.queue_url)
        cdk.CfnOutput(self, "AsgName", value=asg.auto_scaling_group_name)
