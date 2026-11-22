"""AWS-facing logic shared by the CLI and the web app.

Keys in the bucket (per user since v0.2; older jobs live directly under outputs/):
  outputs/<user>/<job_id>.json       status: queued | running | done | error
  outputs/<user>/<job_id>.png        final image
  outputs/<user>/<job_id>.thumb.jpg  preview (newer workers only)

Nothing here keeps state between calls, so it works the same in a local
process and in a Lambda function.
"""
from __future__ import annotations

import json
import random
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

DEFAULT_STACK = "HighDestination"
LOCAL_USER = "local"  # the only user when running locally; also used by the CLI
LEGACY_PREFIX = "outputs/"
SAFE_NAME = re.compile(r"[A-Za-z0-9._@+-]{1,128}")
JOB_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
URL_TTL = 3600


class StackError(RuntimeError):
    pass


def user_prefix(user: str) -> str:
    if not SAFE_NAME.fullmatch(user):
        raise ValueError(f"bad user name {user!r}")
    return f"outputs/{user}/"


def new_job_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-", time.gmtime()) + uuid.uuid4().hex[:6]  # UTC


def slug(text: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n].strip("-") or "wallpaper"


def filename_for(request: dict) -> str:
    return f"{slug(request['prompt'])}-{request['width']}x{request['height']}-{request['seed']}.png"


def build_jobs(*, user: str, prompt: str, width: int, height: int, count: int = 1, seed: int | None = None,
               steps: int = 28, guidance: float = 3.5) -> list[dict]:
    user_prefix(user)  # validates
    jobs = []
    for i in range(count):
        jobs.append({
            "job_id": new_job_id(),
            "user": user,
            "prompt": prompt,
            "width": width,
            "height": height,
            "seed": (seed + i) % 2**32 if seed is not None else random.randrange(2**32),
            "steps": steps,
            "guidance": guidance,
        })
    return jobs


class Stack:
    """The deployed HighDestination stack: bucket, queue and Auto Scaling group."""

    def __init__(self, name: str = DEFAULT_STACK, session: boto3.Session | None = None):
        self.session = session or boto3.Session()
        try:
            stack = self.session.client("cloudformation").describe_stacks(StackName=name)["Stacks"][0]
        except ClientError as e:
            raise StackError(
                f"Cannot read stack {name!r} ({e.response['Error']['Message']}). Deployed? Right region?"
            ) from e
        outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
        self.bucket = outputs["BucketName"]
        self.queue_url = outputs["QueueUrl"]
        self.asg = outputs["AsgName"]
        # SigV4 + the regional endpoint, so presigned URLs work in any region.
        self.s3 = self.session.client(
            "s3", config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"})
        )
        self.sqs = self.session.client("sqs")
        self.autoscaling = self.session.client("autoscaling")

    # ---- worker / queue ------------------------------------------------
    def group(self) -> dict:
        return self.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[self.asg])["AutoScalingGroups"][0]

    def wake(self) -> bool:
        if self.group()["DesiredCapacity"] < 1:
            self.autoscaling.set_desired_capacity(AutoScalingGroupName=self.asg, DesiredCapacity=1)
            return True
        return False

    def stop(self, purge: bool = False) -> None:
        self.autoscaling.set_desired_capacity(AutoScalingGroupName=self.asg, DesiredCapacity=0)
        if purge:
            self.sqs.purge_queue(QueueUrl=self.queue_url)

    def worker(self) -> dict:
        g = self.group()
        info = {"desired": g["DesiredCapacity"], "instance": None}
        if g["Instances"]:
            i = g["Instances"][0]
            info["instance"] = {"id": i["InstanceId"], "type": i.get("InstanceType", "?"),
                                "state": i["LifecycleState"]}
        return info

    def queue(self) -> dict:
        attrs = self.sqs.get_queue_attributes(
            QueueUrl=self.queue_url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        return {"waiting": int(attrs["ApproximateNumberOfMessages"]),
                "in_progress": int(attrs["ApproximateNumberOfMessagesNotVisible"])}

    def latest_log(self, lines: int = 80) -> tuple[str, str] | None:
        objs = self.s3.list_objects_v2(Bucket=self.bucket, Prefix="logs/").get("Contents", [])
        if not objs:
            return None
        latest = max(objs, key=lambda o: o["LastModified"])
        text = self.s3.get_object(Bucket=self.bucket, Key=latest["Key"])["Body"].read().decode(errors="replace")
        return latest["Key"], "\n".join(text.splitlines()[-lines:])

    # ---- jobs ----------------------------------------------------------
    def submit(self, jobs: list[dict]) -> bool:
        """Record each job as queued, put it on SQS, and start a worker. True if one was started."""
        for job in jobs:
            self._put_json(f"{user_prefix(job['user'])}{job['job_id']}.json",
                           {"job_id": job["job_id"], "state": "queued", "request": job, "updated_at": time.time()})
            self.sqs.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps(job))
        return self.wake()

    def job_status(self, user: str, job_id: str, include_legacy: bool = False) -> dict | None:
        """Status dict with an added "_prefix" (where its files are), or None if unknown."""
        prefix = user_prefix(user)
        st = self._get_json(f"{prefix}{job_id}.json")
        if include_legacy and (st is None or st.get("state") == "queued"):
            # A worker from before v0.2 ignores "user" and writes to outputs/<job_id>.*
            old = self._get_json(f"{LEGACY_PREFIX}{job_id}.json")
            if old is not None:
                st, prefix = old, LEGACY_PREFIX
        if st is not None:
            st["_prefix"] = prefix
        return st

    def history(self, user: str, limit: int = 30, before: str | None = None,
                include_legacy: bool = False) -> tuple[list[dict], str | None]:
        """Newest-first page of a user's jobs. Returns (jobs, cursor for the next page)."""
        found: dict[str, str] = {}
        if include_legacy:
            for key in self._list_json(LEGACY_PREFIX, delimiter="/"):
                found[key[len(LEGACY_PREFIX):-5]] = LEGACY_PREFIX
        prefix = user_prefix(user)
        for key in self._list_json(prefix):
            found[key[len(prefix):-5]] = prefix  # the per-user copy wins
        ids = sorted((j for j in found if before is None or j < before), reverse=True)
        page, more = ids[:limit], len(ids) > limit

        def load(job_id: str) -> dict | None:
            st = self._get_json(f"{found[job_id]}{job_id}.json")
            if st is not None:
                st["_prefix"] = found[job_id]
            return st

        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = [j for j in pool.map(load, page) if j is not None]
        return jobs, (page[-1] if more and page else None)

    def delete(self, status: dict) -> None:
        base = f"{status['_prefix']}{status['job_id']}"
        self.s3.delete_objects(Bucket=self.bucket, Delete={
            "Objects": [{"Key": base + ext} for ext in (".json", ".png", ".thumb.jpg")], "Quiet": True})

    def image_key(self, status: dict, thumb: bool = False) -> str:
        ext = ".thumb.jpg" if thumb and status.get("thumb") else ".png"
        return f"{status['_prefix']}{status['job_id']}{ext}"

    def presign(self, key: str, download_name: str | None = None, ttl: int = URL_TTL) -> str:
        params = {"Bucket": self.bucket, "Key": key}
        if download_name:
            params["ResponseContentDisposition"] = f'attachment; filename="{download_name}"'
        return self.s3.generate_presigned_url("get_object", Params=params, ExpiresIn=ttl)

    def download(self, status: dict, dest) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.s3.download_file(self.bucket, self.image_key(status), str(dest))

    # ---- helpers -------------------------------------------------------
    def _get_json(self, key: str) -> dict | None:
        try:
            return json.loads(self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read())
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    def _put_json(self, key: str, body: dict) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=json.dumps(body, indent=2),
                           ContentType="application/json")

    def _list_json(self, prefix: str, delimiter: str | None = None) -> list[str]:
        kwargs = {"Bucket": self.bucket, "Prefix": prefix}
        if delimiter:
            kwargs["Delimiter"] = delimiter
        keys = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(**kwargs):
            keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".json")]
        return keys
