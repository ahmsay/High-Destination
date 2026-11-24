import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "infra" / "fallback")]

import index  # noqa: E402

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def act(status, minutes_ago):
    return {"StatusCode": status, "StartTime": NOW - timedelta(minutes=minutes_ago)}


def test_streak_starts_at_oldest_consecutive_failure():
    acts = [act("InProgress", 0), act("Failed", 1), act("Failed", 10), act("Failed", 25), act("Successful", 300)]
    assert index.streak_start(acts, NOW) == NOW - timedelta(minutes=25)


def test_long_gap_starts_a_new_streak():
    acts = [act("Failed", 2), act("Failed", 5), act("Failed", 5 + 61)]  # yesterday's wake-up is not this one
    assert index.streak_start(acts, NOW) == NOW - timedelta(minutes=5)


def test_no_failures_no_streak():
    assert index.streak_start([act("Successful", 3)], NOW) is None
    assert index.streak_start([], NOW) is None


def test_stays_on_spot_until_threshold_then_alternates(monkeypatch):
    monkeypatch.setattr(index, "FALLBACK_SECONDS", 20 * 60)
    assert index.want_on_demand(False, None) is False
    assert index.want_on_demand(False, 19 * 60) is False
    assert index.want_on_demand(True, 5 * 60) is False  # left over from an earlier wake-up
    assert index.want_on_demand(False, 21 * 60) is True
    assert index.want_on_demand(True, 30 * 60) is False  # On-Demand failed too: try Spot again


class FakeAsg:
    def __init__(self, desired=1, instances=(), on_demand=0, activities=()):
        self.group = {"DesiredCapacity": desired, "Instances": list(instances),
                      "MixedInstancesPolicy": {"InstancesDistribution": {"OnDemandPercentageAboveBaseCapacity": on_demand}}}
        self.activities = list(activities)
        self.updates = []

    def describe_auto_scaling_groups(self, AutoScalingGroupNames):
        return {"AutoScalingGroups": [self.group]}

    def describe_scaling_activities(self, AutoScalingGroupName, MaxRecords):
        return {"Activities": self.activities}

    def update_auto_scaling_group(self, AutoScalingGroupName, MixedInstancesPolicy):
        self.updates.append(MixedInstancesPolicy["InstancesDistribution"]["OnDemandPercentageAboveBaseCapacity"])


def run(monkeypatch, fake, detail_type="EC2 Instance Launch Unsuccessful", minutes_failing=30):
    monkeypatch.setattr(index, "autoscaling", fake)
    monkeypatch.setattr(index, "FALLBACK_SECONDS", 20 * 60)
    real = datetime.now(timezone.utc)
    fake.activities = fake.activities or [{"StatusCode": "Failed", "StartTime": real - timedelta(minutes=m)}
                                          for m in range(0, minutes_failing + 1, 5)]
    index.handler({"detail-type": detail_type}, None)
    return fake.updates


def test_handler_switches_to_on_demand_after_threshold(monkeypatch):
    assert run(monkeypatch, FakeAsg()) == [100]


def test_handler_waits_before_threshold(monkeypatch):
    assert run(monkeypatch, FakeAsg(), minutes_failing=10) == []


def test_handler_resets_to_spot_on_terminate(monkeypatch):
    assert run(monkeypatch, FakeAsg(desired=0, on_demand=100), "EC2 Instance Terminate Successful") == [0]


def test_handler_never_touches_a_running_worker(monkeypatch):
    assert run(monkeypatch, FakeAsg(instances=[{"InstanceId": "i-1"}])) == []
