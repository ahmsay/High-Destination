#!/usr/bin/env python3
"""Context (cdk.json or -c key=value):
  web          true: also deploy the web UI behind a Cognito login
  webDomain    e.g. highdestination.example.com; empty: use the *.cloudfront.net address
  hostedZone   your domain's Route 53 hosted zone in this account (e.g. example.com), if it has one;
               then DNS validation and the site's DNS record are automatic
"""
import os

import aws_cdk as cdk

from high_destination_stack import CertStack, HighDestinationStack


def flag(value) -> bool:
    return str(value).lower() in ("1", "true", "yes")


app = cdk.App()
account = os.environ.get("CDK_DEFAULT_ACCOUNT")
region = os.environ.get("CDK_DEFAULT_REGION")
web = flag(app.node.try_get_context("web"))
domain = (app.node.try_get_context("webDomain") or None) if web else None
hosted_zone = app.node.try_get_context("hostedZone") or None

certificate = None
if domain and region != "us-east-1":
    if not region:
        raise SystemExit("webDomain needs a known region: run cdk with AWS credentials and a default region")
    certificate = CertStack(app, "HighDestinationCert", domain=domain, hosted_zone=hosted_zone,
                            env=cdk.Environment(account=account, region="us-east-1")).certificate

HighDestinationStack(
    app,
    "HighDestination",
    web=web,
    domain=domain,
    hosted_zone=hosted_zone,
    certificate=certificate,
    cross_region_references=certificate is not None,
    env=cdk.Environment(account=account, region=region),
)
app.synth()
