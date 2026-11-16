#!/usr/bin/env python3
import os

import aws_cdk as cdk

from high_destination_stack import HighDestinationStack

app = cdk.App()
HighDestinationStack(
    app,
    "HighDestination",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION"),
    ),
)
app.synth()
