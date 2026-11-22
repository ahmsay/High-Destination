"""Web UI: a JSON API plus the static page in hd/static/.

Run locally with `hd serve`. The app keeps no state of its own (everything is
in S3, SQS and the Auto Scaling group) and images reach the browser as
presigned S3 URLs, so the same app runs on Lambda too (see hd/lambda_handler.py).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import core, sizes
from .auth import User, require_admin, resolver

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_COUNT = 8


class GenRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    preset: Optional[str] = None
    ratio: Optional[str] = None
    size: Optional[str] = None
    count: int = Field(1, ge=1, le=MAX_COUNT)
    seed: Optional[int] = Field(None, ge=0, lt=2**32)
    steps: int = Field(28, ge=1, le=60)
    guidance: float = Field(3.5, ge=0, le=10)

    @field_validator("prompt")
    @classmethod
    def strip_prompt(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("prompt is empty")
        return v

    @field_validator("preset", "ratio", "size")
    @classmethod
    def blank_is_none(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() or None if v is not None else None


def job_view(stack: core.Stack, st: dict) -> dict:
    """What the browser sees of a job: no internal keys, plus image URLs when done."""
    req = st.get("request", {})
    view = {
        "job_id": st["job_id"],
        "state": st.get("state", "queued"),
        "prompt": req.get("prompt"),
        "width": req.get("width"),
        "height": req.get("height"),
        "seed": req.get("seed"),
        "steps": req.get("steps"),
        "guidance": req.get("guidance"),
        "seconds": st.get("seconds"),
        "ratio_clamped": st.get("ratio_clamped", False),
        "error": st.get("error"),
        "updated_at": st.get("updated_at"),
    }
    if view["state"] == "done":
        view["image_url"] = stack.presign(stack.image_key(st))
        view["thumb_url"] = stack.presign(stack.image_key(st, thumb=True))
        view["download_url"] = stack.presign(stack.image_key(st), download_name=core.filename_for(req))
    return view


def create_app(stack: core.Stack, auth_mode: str = "none", client_id: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="High Destination", docs_url="/api/docs", openapi_url="/api/openapi.json")
    current_user = resolver(auth_mode, client_id)
    app.state.current_user = current_user  # tests and future auth modes override this dependency
    admin = require_admin(current_user)

    def check_id(job_id: str) -> str:
        if not core.JOB_ID.fullmatch(job_id):
            raise HTTPException(404, "no such job")
        return job_id

    def find(user: User, job_id: str) -> dict:
        st = stack.job_status(user.name, check_id(job_id), include_legacy=user.is_admin)
        if st is None:
            raise HTTPException(404, "no such job")
        return st

    # Routes are plain `def`, so FastAPI runs the blocking boto3 calls in a thread pool.
    @app.get("/api/me")
    def me(user: User = Depends(current_user)):
        return {"name": user.name, "is_admin": user.is_admin}

    @app.get("/api/presets")
    def presets():
        return {
            "default": "4k",
            "long_side_for_ratio": sizes.LONG_SIDE_FOR_RATIO,
            "max_count": MAX_COUNT,
            "presets": [{"name": n, "width": w, "height": h, "description": d}
                        for n, (w, h, d) in sizes.PRESETS.items()],
        }

    @app.post("/api/jobs", status_code=201)
    def create_jobs(body: GenRequest, user: User = Depends(current_user)):
        try:
            width, height = sizes.resolve(body.preset, body.ratio, body.size)
        except ValueError as e:
            raise HTTPException(422, str(e).replace("--", "")) from e
        jobs = core.build_jobs(user=user.name, prompt=body.prompt, width=width, height=height, count=body.count,
                               seed=body.seed, steps=body.steps, guidance=body.guidance)
        started = stack.submit(jobs)
        return {"worker_started": started,
                "jobs": [job_view(stack, {"job_id": j["job_id"], "state": "queued", "request": j}) for j in jobs]}

    @app.get("/api/jobs")
    def list_jobs(user: User = Depends(current_user), limit: int = Query(30, ge=1, le=100),
                  before: Optional[str] = None):
        if before is not None:
            check_id(before)
        jobs, cursor = stack.history(user.name, limit=limit, before=before, include_legacy=user.is_admin)
        return {"jobs": [job_view(stack, st) for st in jobs], "next": cursor}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, user: User = Depends(current_user)):
        return job_view(stack, find(user, job_id))

    @app.delete("/api/jobs/{job_id}", status_code=204)
    def delete_job(job_id: str, user: User = Depends(current_user)):
        st = find(user, job_id)
        if st.get("state") == "running":
            raise HTTPException(409, "the job is being generated right now; try again when it finishes")
        stack.delete(user.name, job_id, include_legacy=user.is_admin)  # on a queued job this cancels it

    @app.get("/api/worker")
    def worker(_: User = Depends(admin)):
        return {**stack.worker(), "queue": stack.queue()}

    @app.post("/api/worker/stop")
    def stop_worker(purge: bool = False, _: User = Depends(admin)):
        stack.stop(purge=purge)
        return {"stopped": True, "purged": purge}

    @app.get("/api/worker/logs")
    def worker_logs(lines: int = Query(200, ge=1, le=5000), _: User = Depends(admin)):
        log = stack.latest_log(lines)
        return {"key": log[0], "text": log[1]} if log else {"key": None, "text": ""}

    # Login settings for the page. Locally there is no login; in the cloud CDK writes
    # its own config.js next to the page, and this route is never reached.
    @app.get("/config.js", include_in_schema=False)
    def config_js():
        return Response("window.HD_AUTH = null;\n", media_type="text/javascript")

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
