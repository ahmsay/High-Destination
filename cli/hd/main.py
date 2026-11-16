"""hd: generate wallpapers on your own AWS GPU worker.

  hd gen "misty fjord at dawn, cinematic" --preset ultrawide
  hd gen "neon city in rain" --ratio 9:16 --count 4
  hd gen "desert dunes" --size 3440x1440 --seed 42
  hd status | hd fetch <job_id> | hd stop | hd logs | hd presets
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import uuid
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from . import sizes

DEFAULT_STACK = "HighDestination"
DEFAULT_OUT = Path("wallpapers")


class Stack:
    def __init__(self, name: str):
        self.session = boto3.Session()
        try:
            stack = self.session.client("cloudformation").describe_stacks(StackName=name)["Stacks"][0]
        except ClientError as e:
            sys.exit(f"Cannot read stack {name!r} ({e.response['Error']['Message']}). Deployed? Right region?")
        outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
        self.bucket = outputs["BucketName"]
        self.queue_url = outputs["QueueUrl"]
        self.asg = outputs["AsgName"]
        self.s3 = self.session.client("s3")
        self.sqs = self.session.client("sqs")
        self.autoscaling = self.session.client("autoscaling")

    def group(self) -> dict:
        return self.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[self.asg])["AutoScalingGroups"][0]

    def wake(self) -> bool:
        if self.group()["DesiredCapacity"] < 1:
            self.autoscaling.set_desired_capacity(AutoScalingGroupName=self.asg, DesiredCapacity=1)
            return True
        return False

    def job_status(self, job_id: str) -> dict | None:
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=f"outputs/{job_id}.json")["Body"].read()
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        return json.loads(body)

    def download(self, job_id: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.s3.download_file(self.bucket, f"outputs/{job_id}.png", str(dest))
        return dest


def slug(text: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n].strip("-") or "wallpaper"


def worker_state(stack: Stack) -> str:
    instances = stack.group()["Instances"]
    if not instances:
        return "no worker running"
    i = instances[0]
    return f"worker {i['InstanceId']} ({i.get('InstanceType', '?')}) {i['LifecycleState']}"


def wait(stack: Stack, jobs: dict[str, Path], timeout_min: float) -> int:
    pending = dict(jobs)
    deadline = time.time() + timeout_min * 60
    last_line = ""
    failures = 0
    start = time.time()
    while pending and time.time() < deadline:
        for job_id, dest in list(pending.items()):
            st = stack.job_status(job_id)
            if not st or st["state"] == "running":
                continue
            del pending[job_id]
            if st["state"] == "done":
                stack.download(job_id, dest)
                extra = " (ratio wider than 2.5:1 was cropped)" if st.get("ratio_clamped") else ""
                print(f"\n✔ {dest}  {st['size'][0]}x{st['size'][1]}  seed {st['request']['seed']}"
                      f"  {st.get('seconds')}s{extra}")
            else:
                failures += 1
                print(f"\n✘ {job_id}: {st.get('error')}")
        if pending:
            states = [(stack.job_status(j) or {}).get("state", "queued") for j in pending]
            line = (f"  {int(time.time() - start)}s  {len(pending)} left "
                    f"[{', '.join(sorted(set(states)))}]  {worker_state(stack)}")
            if line != last_line:
                print("\r" + line.ljust(100), end="", flush=True)
                last_line = line
            time.sleep(5)
    if pending:
        print(f"\nStill waiting after {timeout_min} min. The jobs stay queued; later run:")
        for job_id in pending:
            print(f"  hd fetch {job_id}")
        print("If nothing happens, check `hd logs` (the first boot downloads ~17 GB of weights).")
        return 1
    return 1 if failures else 0


def cmd_gen(args, stack: Stack) -> int:
    try:
        width, height = sizes.resolve(args.preset, args.ratio, args.size)
    except ValueError as e:
        sys.exit(str(e))
    jobs: dict[str, Path] = {}
    for i in range(args.count):
        job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        seed = args.seed + i if args.seed is not None else random.randrange(2**32)
        job = {"job_id": job_id, "prompt": args.prompt, "width": width, "height": height,
               "seed": seed, "steps": args.steps, "guidance": args.guidance}
        stack.sqs.send_message(QueueUrl=stack.queue_url, MessageBody=json.dumps(job))
        jobs[job_id] = args.out / f"{slug(args.prompt)}-{width}x{height}-{seed}.png"
        print(f"queued {job_id}  {width}x{height}  seed {seed}")
    if stack.wake():
        print("starting a GPU worker (cold start is a few minutes)")
    if args.no_wait:
        return 0
    return wait(stack, jobs, args.timeout)


def cmd_fetch(args, stack: Stack) -> int:
    st = stack.job_status(args.job_id)
    if not st:
        print("no result yet (still queued)")
        return 1
    if st["state"] != "done":
        print(json.dumps(st, indent=2))
        return 1
    req = st["request"]
    dest = args.out / f"{slug(req['prompt'])}-{req['width']}x{req['height']}-{req['seed']}.png"
    print(stack.download(args.job_id, dest))
    return 0


def cmd_status(args, stack: Stack) -> int:
    attrs = stack.sqs.get_queue_attributes(
        QueueUrl=stack.queue_url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    print(f"queue: {attrs['ApproximateNumberOfMessages']} waiting, "
          f"{attrs['ApproximateNumberOfMessagesNotVisible']} in progress")
    g = stack.group()
    print(f"asg:   desired {g['DesiredCapacity']}, {worker_state(stack)}")
    return 0


def cmd_stop(args, stack: Stack) -> int:
    stack.autoscaling.set_desired_capacity(AutoScalingGroupName=stack.asg, DesiredCapacity=0)
    print("desired capacity set to 0; the worker will be terminated")
    if args.purge:
        stack.sqs.purge_queue(QueueUrl=stack.queue_url)
        print("queue purged")
    return 0


def cmd_logs(args, stack: Stack) -> int:
    objs = stack.s3.list_objects_v2(Bucket=stack.bucket, Prefix="logs/").get("Contents", [])
    if not objs:
        print("no logs yet (uploaded when a worker shuts down; use SSM Session Manager on a live one)")
        return 1
    latest = max(objs, key=lambda o: o["LastModified"])
    print(f"== {latest['Key']} ==")
    text = stack.s3.get_object(Bucket=stack.bucket, Key=latest["Key"])["Body"].read().decode(errors="replace")
    print("\n".join(text.splitlines()[-args.lines:]))
    return 0


def cmd_presets(args) -> int:
    for name, (w, h, desc) in sizes.PRESETS.items():
        print(f"  {name:<14} {w:>5}x{h:<5} {desc}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hd", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stack", default=DEFAULT_STACK, help="CloudFormation stack name")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen", help="generate wallpaper(s)")
    g.add_argument("prompt")
    g.add_argument("--preset", "-p", help="named size, see `hd presets` (default: 4k)")
    g.add_argument("--ratio", "-r", help="aspect ratio, e.g. 16:9, 21:9, 9:16 (long side 3840)")
    g.add_argument("--size", "-s", help="exact pixels, e.g. 3440x1440")
    g.add_argument("--count", "-n", type=int, default=1, help="number of variations")
    g.add_argument("--seed", type=int, help="seed (incremented for each variation)")
    g.add_argument("--steps", type=int, default=28)
    g.add_argument("--guidance", type=float, default=3.5)
    g.add_argument("--out", "-o", type=Path, default=DEFAULT_OUT, help="download folder (default: ./wallpapers)")
    g.add_argument("--timeout", type=float, default=30, help="minutes to wait (default 30)")
    g.add_argument("--no-wait", action="store_true", help="queue and exit; use `hd fetch` later")

    f = sub.add_parser("fetch", help="download a finished job")
    f.add_argument("job_id")
    f.add_argument("--out", "-o", type=Path, default=DEFAULT_OUT)

    sub.add_parser("status", help="queue and worker state")
    s = sub.add_parser("stop", help="terminate the worker now")
    s.add_argument("--purge", action="store_true", help="also drop queued jobs")
    lg = sub.add_parser("logs", help="show the latest worker log")
    lg.add_argument("--lines", type=int, default=80)
    sub.add_parser("presets", help="list size presets")

    args = p.parse_args(argv)
    if args.cmd == "presets":
        return cmd_presets(args)
    stack = Stack(args.stack)
    return {"gen": cmd_gen, "fetch": cmd_fetch, "status": cmd_status, "stop": cmd_stop, "logs": cmd_logs}[args.cmd](
        args, stack
    )


if __name__ == "__main__":
    sys.exit(main())
