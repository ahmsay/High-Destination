"""Web API + shared core, against moto's fake S3/SQS and a stub Auto Scaling group."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cli"))

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from hd import core, web  # noqa: E402

BUCKET = "hd-test"


class FakeAsg:
    def __init__(self):
        self.desired = 0
        self.instances = []

    def describe_auto_scaling_groups(self, AutoScalingGroupNames):
        return {"AutoScalingGroups": [{"DesiredCapacity": self.desired, "Instances": self.instances}]}

    def set_desired_capacity(self, AutoScalingGroupName, DesiredCapacity):
        self.desired = DesiredCapacity


@pytest.fixture
def stack(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with moto.mock_aws():
        session = boto3.Session()
        s = core.Stack.__new__(core.Stack)  # skip the CloudFormation lookup
        s.session = session
        s.s3 = session.client("s3")
        s.s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        s.sqs = session.client("sqs")
        s.bucket, s.asg = BUCKET, "asg"
        s.queue_url = s.sqs.create_queue(QueueName="jobs")["QueueUrl"]
        s.autoscaling = FakeAsg()
        yield s


@pytest.fixture
def client(stack):
    return TestClient(web.create_app(stack, auth_mode="none"))


def finish(stack, job_id, prefix="outputs/local/", thumb=True, **extra):
    """Pretend to be the worker."""
    st = json.loads(stack.s3.get_object(Bucket=BUCKET, Key=f"outputs/local/{job_id}.json")["Body"].read())
    body = {"job_id": job_id, "state": "done", "request": st["request"], "size": [st["request"]["width"],
            st["request"]["height"]], "seconds": 40.1, "thumb": thumb, **extra}
    stack.s3.put_object(Bucket=BUCKET, Key=f"{prefix}{job_id}.json", Body=json.dumps(body))
    stack.s3.put_object(Bucket=BUCKET, Key=f"{prefix}{job_id}.png", Body=b"png")
    if thumb:
        stack.s3.put_object(Bucket=BUCKET, Key=f"{prefix}{job_id}.thumb.jpg", Body=b"jpg")


def test_page_and_config(client):
    assert "High Destination" in client.get("/").text
    assert client.get("/app.js").status_code == 200
    assert client.get("/api/me").json() == {"name": "local", "is_admin": True}
    names = [p["name"] for p in client.get("/api/presets").json()["presets"]]
    assert "ultrawide" in names


def test_generate_queues_records_and_wakes(client, stack):
    r = client.post("/api/jobs", json={"prompt": " fjord ", "preset": "ultrawide", "count": 2, "seed": 7})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["worker_started"] and stack.autoscaling.desired == 1
    jobs = body["jobs"]
    assert [j["seed"] for j in jobs] == [7, 8]
    assert all(j["state"] == "queued" and (j["width"], j["height"]) == (3440, 1440) for j in jobs)

    msgs = stack.sqs.receive_message(QueueUrl=stack.queue_url, MaxNumberOfMessages=10)["Messages"]
    sent = [json.loads(m["Body"]) for m in msgs]
    assert {m["user"] for m in sent} == {"local"} and {m["prompt"] for m in sent} == {"fjord"}

    # queued jobs show up in history right away (written by the API, not the worker)
    listed = client.get("/api/jobs").json()["jobs"]
    assert {j["job_id"] for j in listed} == {j["job_id"] for j in jobs}

    # second submit does not wake again
    assert not client.post("/api/jobs", json={"prompt": "x"}).json()["worker_started"]


@pytest.mark.parametrize("body", [
    {"prompt": "   "},
    {"prompt": "x", "count": 9},
    {"prompt": "x", "preset": "4k", "ratio": "16:9"},
    {"prompt": "x", "preset": "nope"},
    {"prompt": "x", "size": "10x10"},
    {"prompt": "x", "steps": 0},
])
def test_generate_rejects_bad_input(client, stack, body):
    assert client.post("/api/jobs", json=body).status_code == 422
    assert stack.autoscaling.desired == 0


def test_done_job_has_presigned_urls(client, stack):
    job_id = client.post("/api/jobs", json={"prompt": "dunes", "size": "2560x1080", "seed": 3}).json()["jobs"][0]["job_id"]
    assert client.get(f"/api/jobs/{job_id}").json()["state"] == "queued"
    finish(stack, job_id)
    j = client.get(f"/api/jobs/{job_id}").json()
    assert j["state"] == "done" and j["seconds"] == 40.1
    assert f"outputs/local/{job_id}.png" in j["image_url"]
    assert f"{job_id}.thumb.jpg" in j["thumb_url"]
    assert "dunes-2560x1080-3.png" in j["download_url"]
    assert "_prefix" not in j


def test_old_worker_output_is_found(client, stack):
    """A worker deployed before v0.2 ignores "user" and writes to outputs/<job_id>.*"""
    job_id = client.post("/api/jobs", json={"prompt": "old"}).json()["jobs"][0]["job_id"]
    finish(stack, job_id, prefix="outputs/", thumb=False)
    j = client.get(f"/api/jobs/{job_id}").json()
    assert j["state"] == "done"
    assert f"outputs/{job_id}.png" in j["image_url"] and f"outputs/{job_id}.png" in j["thumb_url"]


def test_history_pages_and_includes_legacy(client, stack):
    ids = [client.post("/api/jobs", json={"prompt": f"p{i}"}).json()["jobs"][0]["job_id"] for i in range(3)]
    legacy = "20200101-000000-aaaaaa"
    stack.s3.put_object(Bucket=BUCKET, Key=f"outputs/{legacy}.json", Body=json.dumps(
        {"job_id": legacy, "state": "done", "request": {"prompt": "old", "width": 1, "height": 1, "seed": 1}}))
    first = client.get("/api/jobs?limit=2").json()
    assert len(first["jobs"]) == 2 and first["next"]
    rest = client.get(f"/api/jobs?limit=10&before={first['next']}").json()
    seen = [j["job_id"] for j in first["jobs"] + rest["jobs"]]
    assert seen == sorted(ids, reverse=True) + [legacy]
    assert rest["next"] is None


def test_delete(client, stack):
    job_id = client.post("/api/jobs", json={"prompt": "bye"}).json()["jobs"][0]["job_id"]
    finish(stack, job_id)
    assert client.delete(f"/api/jobs/{job_id}").status_code == 204
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert "Contents" not in stack.s3.list_objects_v2(Bucket=BUCKET, Prefix="outputs/")


def test_cancel_queued_but_not_running(client, stack):
    job_id = client.post("/api/jobs", json={"prompt": "nah"}).json()["jobs"][0]["job_id"]
    assert client.delete(f"/api/jobs/{job_id}").status_code == 204  # queued -> cancelled
    assert client.get("/api/jobs").json()["jobs"] == []
    job_id = client.post("/api/jobs", json={"prompt": "busy"}).json()["jobs"][0]["job_id"]
    key = f"outputs/local/{job_id}.json"
    st = json.loads(stack.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    stack.s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps({**st, "state": "running"}))
    assert client.delete(f"/api/jobs/{job_id}").status_code == 409


def test_deleting_old_worker_result_does_not_leave_a_ghost(client, stack):
    """Regression: an old worker wrote outputs/<id>.*, delete removed only those, and the
    API's own outputs/local/<id>.json "queued" record came back as a job stuck in the queue."""
    ids = [j["job_id"] for j in client.post("/api/jobs", json={"prompt": "x", "count": 2}).json()["jobs"]]
    for job_id in ids:
        finish(stack, job_id, prefix="outputs/", thumb=False)
    listed = client.get("/api/jobs").json()["jobs"]
    assert {j["state"] for j in listed} == {"done"}  # history prefers the finished result
    assert client.delete(f"/api/jobs/{ids[0]}").status_code == 204
    assert [j["job_id"] for j in client.get("/api/jobs").json()["jobs"]] == [ids[1]]
    assert client.get(f"/api/jobs/{ids[0]}").status_code == 404


def test_worker_skips_cancelled_jobs(stack, monkeypatch):
    pytest.importorskip("PIL")
    monkeypatch.setenv("HD_BUCKET", BUCKET)
    monkeypatch.setenv("HD_QUEUE_URL", stack.queue_url)
    monkeypatch.setenv("HD_COMFY_DIR", "/nonexistent")
    monkeypatch.syspath_prepend(str(ROOT / "worker"))
    import importlib
    worker = importlib.import_module("worker")
    worker = importlib.reload(worker)  # bind its clients to this test's moto
    assert worker.status_exists("outputs/local/", "nope") is False
    stack.s3.put_object(Bucket=BUCKET, Key="outputs/local/yes.json", Body=b"{}")
    assert worker.status_exists("outputs/local/", "yes") is True
    assert worker.prefix_for({"user": "local"}) == "outputs/local/"
    assert worker.prefix_for({}) == "outputs/"
    with pytest.raises(ValueError):
        worker.prefix_for({"user": "../etc"})


def test_bad_ids_are_404(client):
    assert client.get("/api/jobs/..%2F..%2Fsecret").status_code == 404
    assert client.get("/api/jobs/20260101-000000-zzzzzz").status_code == 404


def test_worker_controls(client, stack):
    stack.autoscaling.desired = 1
    stack.autoscaling.instances = [{"InstanceId": "i-1", "InstanceType": "g6.xlarge", "LifecycleState": "InService"}]
    w = client.get("/api/worker").json()
    assert w["instance"]["type"] == "g6.xlarge" and w["queue"] == {"waiting": 0, "in_progress": 0}
    assert client.post("/api/worker/stop").json()["stopped"]
    assert stack.autoscaling.desired == 0
    assert client.get("/api/worker/logs").json() == {"key": None, "text": ""}
    stack.s3.put_object(Bucket=BUCKET, Key="logs/i-1.log", Body=b"a\nb\nc")
    assert client.get("/api/worker/logs?lines=2").json()["text"] == "b\nc"


def test_non_admin_is_locked_out_of_worker(stack):
    from hd import auth

    app = web.create_app(stack, auth_mode="none")
    guest = auth.User("guest", is_admin=False)
    app.dependency_overrides[app.state.current_user] = lambda: guest
    c = TestClient(app)
    assert c.get("/api/me").json()["name"] == "guest"
    assert c.get("/api/worker").status_code == 403
    assert c.post("/api/worker/stop").status_code == 403
    job_id = c.post("/api/jobs", json={"prompt": "mine"}).json()["jobs"][0]["job_id"]
    assert stack.s3.get_object(Bucket=BUCKET, Key=f"outputs/guest/{job_id}.json")


def test_user_names_are_validated():
    with pytest.raises(ValueError):
        core.user_prefix("../x")
    with pytest.raises(ValueError):
        core.build_jobs(user="a/b", prompt="x", width=64, height=64)
