"""Job loop: SQS -> ComfyUI -> resize/crop -> S3. Exits after HD_IDLE_MINUTES without work.

Job message (JSON):
  {"job_id": "...", "user": "local", "prompt": "...", "width": 3840, "height": 2160,
   "seed": 123, "steps": 28, "guidance": 3.5}

Results in S3 (<prefix> is outputs/<user>/, or outputs/ for jobs without a user):
  <prefix><job_id>.json       status + metadata (state: queued | running | done | error)
  <prefix><job_id>.png        final image (when done)
  <prefix><job_id>.thumb.jpg  small preview for galleries (when done)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import boto3
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sizing  # noqa: E402
import workflow  # noqa: E402

BUCKET = os.environ["HD_BUCKET"]
QUEUE_URL = os.environ["HD_QUEUE_URL"]
IDLE_SECONDS = int(os.environ.get("HD_IDLE_MINUTES", "10")) * 60
COMFY_DIR = Path(os.environ["HD_COMFY_DIR"])
COMFY_URL = "http://127.0.0.1:8188"
INSTANCE_ID = os.environ.get("HD_INSTANCE_ID", "local")

s3 = boto3.client("s3")
sqs = boto3.client("sqs")


def log(msg: str) -> None:
    print(f"[worker {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def comfy(path: str, payload: dict | None = None, timeout: float = 30) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(COMFY_URL + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ComfyUI {path} -> {e.code}: {e.read().decode(errors='replace')[:2000]}") from e


def wait_for_comfy(timeout: float = 600) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            comfy("/system_stats", timeout=5)
            log("ComfyUI is up")
            return
        except Exception:
            time.sleep(3)
    raise RuntimeError("ComfyUI did not start in time")


THUMB_SIZE = 640
SAFE_NAME = re.compile(r"[A-Za-z0-9._@+-]{1,128}")


def prefix_for(job: dict) -> str:
    user = job.get("user")
    if user is None:
        return "outputs/"
    if not SAFE_NAME.fullmatch(str(user)):
        raise ValueError(f"bad user name {user!r}")
    return f"outputs/{user}/"


def put_status(prefix: str, job_id: str, **fields) -> None:
    body = {"job_id": job_id, "instance_id": INSTANCE_ID, "updated_at": time.time(), **fields}
    s3.put_object(
        Bucket=BUCKET, Key=f"{prefix}{job_id}.json", Body=json.dumps(body, indent=2), ContentType="application/json"
    )


def run_job(job: dict, prefix: str) -> dict:
    job_id = job["job_id"]
    p = sizing.plan(int(job["width"]), int(job["height"]))
    log(f"{job_id}: target {p.target_w}x{p.target_h}, generate {p.gen_w}x{p.gen_h}, upscale={p.upscale}")
    wf = workflow.build(
        prompt=job["prompt"],
        width=p.gen_w,
        height=p.gen_h,
        seed=int(job["seed"]),
        steps=int(job.get("steps", 28)),
        guidance=float(job.get("guidance", 3.5)),
        upscale=p.upscale,
        filename_prefix=job_id,
    )
    started = time.time()
    prompt_id = comfy("/prompt", {"prompt": wf, "client_id": "high-destination"})["prompt_id"]

    while True:
        time.sleep(2)
        hist = comfy(f"/history/{prompt_id}").get(prompt_id)
        if not hist:
            if time.time() - started > 12 * 60:
                raise RuntimeError("generation timed out")
            continue
        status = hist.get("status", {})
        if status.get("status_str") == "error":
            raise RuntimeError(f"ComfyUI error: {json.dumps(status.get('messages'))[:2000]}")
        if status.get("completed") or hist.get("outputs"):
            break

    img_info = hist["outputs"][workflow.SAVE_NODE]["images"][0]
    src = COMFY_DIR / "output" / img_info.get("subfolder", "") / img_info["filename"]
    with Image.open(src) as im:
        final = sizing.fit(im.convert("RGB"), p.target_w, p.target_h)
    out = Path("/tmp") / f"{job_id}.png"
    final.save(out, optimize=False)
    s3.upload_file(str(out), BUCKET, f"{prefix}{job_id}.png", ExtraArgs={"ContentType": "image/png"})
    out.unlink(missing_ok=True)
    thumb = final.copy()
    thumb.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
    thumb_out = Path("/tmp") / f"{job_id}.thumb.jpg"
    thumb.save(thumb_out, quality=85)
    s3.upload_file(str(thumb_out), BUCKET, f"{prefix}{job_id}.thumb.jpg", ExtraArgs={"ContentType": "image/jpeg"})
    thumb_out.unlink(missing_ok=True)
    src.unlink(missing_ok=True)
    return {
        "gen_size": [p.gen_w, p.gen_h],
        "size": [p.target_w, p.target_h],
        "ratio_clamped": p.ratio_clamped,
        "seconds": round(time.time() - started, 1),
        "thumb": True,
    }


def main() -> None:
    wait_for_comfy()
    last_work = time.time()
    while True:
        resp = sqs.receive_message(QueueUrl=QUEUE_URL, MaxNumberOfMessages=1, WaitTimeSeconds=20)
        msgs = resp.get("Messages", [])
        if not msgs:
            if time.time() - last_work > IDLE_SECONDS:
                log("idle, exiting")
                return
            continue
        msg = msgs[0]
        try:
            job = json.loads(msg["Body"])
            job_id = job["job_id"]
            if not SAFE_NAME.fullmatch(str(job_id)):
                raise ValueError("bad job id")
            prefix = prefix_for(job)
        except Exception:
            log(f"dropping malformed message: {msg['Body'][:200]}")
            sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])
            continue

        put_status(prefix, job_id, state="running", request=job)
        try:
            meta = run_job(job, prefix)
            put_status(prefix, job_id, state="done", request=job, **meta)
            log(f"{job_id}: done in {meta['seconds']}s")
        except Exception as e:
            traceback.print_exc()
            put_status(prefix, job_id, state="error", request=job, error=str(e))
        # Errors are recorded and not retried (they are usually deterministic).
        # A Spot interruption kills the process before this line, so the job
        # becomes visible again and is retried on the replacement instance.
        sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])
        last_work = time.time()


if __name__ == "__main__":
    main()
