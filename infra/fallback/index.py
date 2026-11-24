"""Spot -> On-Demand fallback for the worker Auto Scaling group.

Runs on the group's EventBridge events:
  - "EC2 Instance Launch Unsuccessful": once launches have been failing for
    HD_FALLBACK_MINUTES, alternate the group between On-Demand and Spot on each
    further failure, so both get tried while either is unavailable.
  - "EC2 Instance Terminate Successful": back to Spot for the next wake-up.

The mode is only changed while the group has no instance, so a running worker
is never replaced. The deployed template is Spot; a deploy may reset it.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import boto3

ASG = os.environ.get("HD_ASG", "")
FALLBACK_SECONDS = int(os.environ.get("HD_FALLBACK_MINUTES", "20")) * 60
# Failures further apart than this belong to separate wake-ups.
STREAK_GAP_SECONDS = 3600

autoscaling = boto3.client("autoscaling") if ASG else None


def streak_start(activities: list[dict], now: datetime) -> datetime | None:
    """Start of the current run of failed launches (activities newest first), or None."""
    start, newer = None, now
    for a in activities:
        status = a["StatusCode"]
        if status == "Failed":
            if (newer - a["StartTime"]).total_seconds() > STREAK_GAP_SECONDS:
                break
            start = newer = a["StartTime"]
        elif status in ("Successful", "Cancelled"):
            break
        # Anything else is a launch still in progress: skip it.
    return start


def want_on_demand(on_demand: bool, failing_seconds: float | None) -> bool:
    if failing_seconds is None or failing_seconds < FALLBACK_SECONDS:
        return False
    return not on_demand  # past the threshold: alternate, so Spot is still tried too


def is_on_demand(group: dict) -> bool:
    dist = group.get("MixedInstancesPolicy", {}).get("InstancesDistribution", {})
    return dist.get("OnDemandPercentageAboveBaseCapacity", 0) > 0


def set_on_demand(on: bool) -> None:
    autoscaling.update_auto_scaling_group(
        AutoScalingGroupName=ASG,
        MixedInstancesPolicy={"InstancesDistribution": {"OnDemandPercentageAboveBaseCapacity": 100 if on else 0}},
    )
    print(f"worker now launches {'On-Demand' if on else 'Spot'} instances")


def handler(event, context):
    group = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG])["AutoScalingGroups"][0]
    if group["Instances"]:
        return  # never change the mode under a running worker
    on_demand = is_on_demand(group)

    if event.get("detail-type") == "EC2 Instance Terminate Successful" or group["DesiredCapacity"] < 1:
        want = False
    else:
        now = datetime.now(timezone.utc)
        acts = autoscaling.describe_scaling_activities(AutoScalingGroupName=ASG, MaxRecords=50)["Activities"]
        start = streak_start(acts, now)
        failing = (now - start).total_seconds() if start else None
        print(f"launches failing for {failing and round(failing / 60)} min; on_demand={on_demand}")
        want = want_on_demand(on_demand, failing)

    if want != on_demand:
        set_on_demand(want)
