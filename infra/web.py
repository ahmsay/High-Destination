"""The cloud-hosted web UI, for you only, behind a Cognito login.

  CloudFront (https://<domain>/)
    /*      -> private S3 site bucket: cli/hd/static + a generated config.js
    /api/*  -> API Gateway HTTP API (JWT authorizer: Cognito access token)
                 -> Lambda running the same FastAPI app as `hd serve` (via Mangum)
  Cognito user pool: sign in with email + password (no usernames), no self
  sign-up, TOTP MFA required, managed login page branded to match the app (login_branding.py).
  Members of the "admin" group are the owner: they see the worker controls and
  share outputs/local/ with the CLI.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import aws_cdk as cdk
import jsii
from aws_cdk import (
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_authorizers as authorizers,
    aws_apigatewayv2_integrations as integrations,
    aws_autoscaling as autoscaling,
    aws_certificatemanager as acm,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_route53 as route53,
    aws_route53_targets as targets,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_sqs as sqs,
)
from constructs import Construct

from login_branding import settings as login_branding_settings

CLI_DIR = Path(__file__).resolve().parent.parent / "cli"
STATIC_DIR = CLI_DIR / "hd" / "static"
# Lambda already ships boto3, so only the web framework goes in the bundle.
LAMBDA_REQUIREMENTS = ["fastapi>=0.110", "mangum>=0.17"]
ADMIN_GROUP = "admin"


@jsii.implements(cdk.ILocalBundling)
class _PipBundling:
    """Bundle the API without Docker: pip downloads Linux arm64 wheels for Python 3.12."""

    def try_bundle(self, output_dir: str, *args, **kwargs) -> bool:
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                 "--target", output_dir, "--platform", "manylinux2014_aarch64", "--implementation", "cp",
                 "--python-version", "3.12", "--only-binary=:all:", *LAMBDA_REQUIREMENTS],
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as e:
            print(f"local bundling failed ({e}); falling back to Docker", file=sys.stderr)
            return False
        shutil.copytree(CLI_DIR / "hd", Path(output_dir) / "hd", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
        return True


class WebApp(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        bucket: s3.IBucket,
        queue: sqs.IQueue,
        asg: autoscaling.AutoScalingGroup,
        worker_tag: tuple[str, str],
        domain: Optional[str] = None,
        certificate: Optional[acm.ICertificate] = None,
        zone: Optional[route53.IHostedZone] = None,
        auth_domain_prefix: Optional[str] = None,
    ) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)

        # ---- Login ---------------------------------------------------------
        pool = cognito.UserPool(
            self,
            "Users",
            self_sign_up_enabled=False,  # the only accounts are the ones you create
            # Email is the username: you sign in with email + password. Changing this replaces the pool.
            sign_in_aliases=cognito.SignInAliases(username=False, email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True)
            ),
            mfa=cognito.Mfa.REQUIRED,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            password_policy=cognito.PasswordPolicy(min_length=12),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            feature_plan=cognito.FeaturePlan.ESSENTIALS,  # managed login + branding
            user_invitation=cognito.UserInvitationConfig(
                email_subject="Your High Destination login",
                email_body="Sign in with your email address ({username}) and the temporary password {####}",
            ),
            removal_policy=cdk.RemovalPolicy.DESTROY,  # holds nothing but your login
        )
        cognito.CfnUserPoolGroup(self, "AdminGroup", user_pool_id=pool.user_pool_id, group_name=ADMIN_GROUP,
                                 description="Owner: worker controls, shares outputs/local/ with the CLI")
        # Must differ from the old (username) pool's prefix: CloudFormation creates the
        # replacement domain before it deletes the old one, and prefixes are unique.
        prefix = auth_domain_prefix or f"highdestination-login-{stack.account}"
        pool.add_domain("LoginDomain", cognito_domain=cognito.CognitoDomainOptions(domain_prefix=prefix),
                        managed_login_version=cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN)
        login_domain = f"{prefix}.auth.{stack.region}.amazoncognito.com"

        # ---- API -----------------------------------------------------------
        fn = lambda_.Function(
            self,
            "Api",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="hd.lambda_handler.handler",
            code=lambda_.Code.from_asset(
                str(CLI_DIR),
                exclude=["*.egg-info", "**/__pycache__"],
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    platform="linux/arm64",
                    local=_PipBundling(),
                    command=["bash", "-c", "pip install -q -t /asset-output "
                             + " ".join(f"'{r}'" for r in LAMBDA_REQUIREMENTS) + " && cp -r hd /asset-output/"],
                ),
            ),
            memory_size=512,
            timeout=cdk.Duration.seconds(29),  # API Gateway gives up at 30 s
            log_group=logs.LogGroup(self, "ApiLogs", retention=logs.RetentionDays.TWO_WEEKS,
                                    removal_policy=cdk.RemovalPolicy.DESTROY),
            environment={
                "HD_BUCKET": bucket.bucket_name,
                "HD_QUEUE_URL": queue.queue_url,
                "HD_ASG": asg.auto_scaling_group_name,
            },
        )
        bucket.grant_read_write(fn, "outputs/*")
        bucket.grant_read(fn, "logs/*")
        queue.grant_send_messages(fn)
        fn.add_to_role_policy(iam.PolicyStatement(actions=["sqs:PurgeQueue"], resources=[queue.queue_arn]))
        fn.add_to_role_policy(iam.PolicyStatement(actions=["autoscaling:DescribeAutoScalingGroups"], resources=["*"]))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["autoscaling:SetDesiredCapacity"],
            resources=["*"],
            conditions={"StringEquals": {f"autoscaling:ResourceTag/{worker_tag[0]}": worker_tag[1]}},
        ))

        http_api = apigw.HttpApi(self, "HttpApi", create_default_stage=False)
        http_api.add_stage(
            "Stage",
            stage_name="$default",
            auto_deploy=True,
            # One person polling every few seconds needs far less; this caps the bill if a token leaks.
            throttle=apigw.ThrottleSettings(rate_limit=10, burst_limit=20),
        )
        api_origin_host = f"{http_api.api_id}.execute-api.{stack.region}.{stack.url_suffix}"

        # ---- Site + CDN ----------------------------------------------------
        site = s3.Bucket(
            self,
            "Site",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,  # only copies of cli/hd/static
        )
        images = (f"https://{bucket.bucket_name}.s3.{stack.region}.{stack.url_suffix} "
                  f"https://{bucket.bucket_name}.s3.{stack.url_suffix}")  # presigned image URLs
        csp = "; ".join([
            "default-src 'none'",
            "script-src 'self'",
            "style-src 'self'",
            f"img-src 'self' data: {images}",
            f"connect-src 'self' https://{login_domain}",
            "form-action 'self'",
            "base-uri 'none'",
            "frame-ancestors 'none'",
        ])
        headers = cloudfront.ResponseHeadersPolicy(
            self,
            "Headers",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=csp, override=True),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=cdk.Duration.days(365), include_subdomains=False, override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.SAME_ORIGIN, override=True),
            ),
        )
        # API responses are never cached: TTL 0 unless the app says otherwise (it does not).
        # Authorization is in the key so CloudFront passes it on to API Gateway.
        api_cache = cloudfront.CachePolicy(
            self,
            "ApiCache",
            default_ttl=cdk.Duration.seconds(0),
            min_ttl=cdk.Duration.seconds(0),
            max_ttl=cdk.Duration.seconds(1),
            header_behavior=cloudfront.CacheHeaderBehavior.allow_list("Authorization"),
            query_string_behavior=cloudfront.CacheQueryStringBehavior.all(),
            cookie_behavior=cloudfront.CacheCookieBehavior.none(),
        )
        dist = cloudfront.Distribution(
            self,
            "Cdn",
            comment="High Destination web UI",
            default_root_object="index.html",
            domain_names=[domain] if domain else None,
            certificate=certificate,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=headers,
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=origins.HttpOrigin(api_origin_host),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=api_cache,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    response_headers_policy=headers,
                ),
            },
        )
        site_url = f"https://{domain or dist.distribution_domain_name}/"

        client = pool.add_client(
            "Web",
            generate_secret=False,  # a browser cannot keep a secret; PKCE protects the code instead
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID],
                callback_urls=[site_url],
                logout_urls=[site_url],
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            prevent_user_existence_errors=True,
            access_token_validity=cdk.Duration.hours(1),
            id_token_validity=cdk.Duration.hours(1),
            refresh_token_validity=cdk.Duration.days(30),
        )
        # Managed login has no default style: without one the page shows an error.
        cognito.CfnManagedLoginBranding(self, "LoginBranding", user_pool_id=pool.user_pool_id,
                                        client_id=client.user_pool_client_id,
                                        settings=login_branding_settings())
        fn.add_environment("HD_CLIENT_ID", client.user_pool_client_id)
        http_api.add_routes(
            path="/api/{proxy+}",
            methods=[apigw.HttpMethod.ANY],
            integration=integrations.HttpLambdaIntegration("Fn", fn),
            authorizer=authorizers.HttpUserPoolAuthorizer("Cognito", pool, user_pool_clients=[client]),
        )

        config = stack.to_json_string({"domain": login_domain, "clientId": client.user_pool_client_id,
                                       "redirectUri": site_url})
        s3deploy.BucketDeployment(
            self,
            "Deploy",
            destination_bucket=site,
            sources=[s3deploy.Source.asset(str(STATIC_DIR)),
                     s3deploy.Source.data("config.js", f"window.HD_AUTH = {config};\n")],
            cache_control=[s3deploy.CacheControl.from_string("no-cache")],  # revalidate; files are tiny
            distribution=dist,
            distribution_paths=["/*"],
            memory_limit=256,
        )

        if domain and zone:
            for i, record in enumerate((route53.ARecord, route53.AaaaRecord)):
                record(self, f"Alias{i}", zone=zone, record_name=domain,
                       target=route53.RecordTarget.from_alias(targets.CloudFrontTarget(dist)))

        cdk.CfnOutput(stack, "WebUrl", value=site_url)
        cdk.CfnOutput(stack, "UserPoolId", value=pool.user_pool_id)
        cdk.CfnOutput(stack, "LoginDomain", value=login_domain)
        if domain and not zone:
            cdk.CfnOutput(stack, "DnsCname", value=dist.distribution_domain_name,
                          description=f"Point a CNAME record for {domain} at this")
