"""hd: generate wallpapers on your own AWS GPU worker.

  hd gen "misty fjord at dawn, cinematic" --preset ultrawide
  hd gen "neon city in rain" --ratio 9:16 --count 4
  hd gen "desert dunes" --size 3440x1440 --seed 42
  hd serve                      # web UI on http://127.0.0.1:8000
  hd status | hd fetch <job_id> | hd stop | hd logs | hd presets
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import core, sizes

DEFAULT_OUT = Path("wallpapers")
USER = core.LOCAL_USER


def open_stack(name: str) -> core.Stack:
    try:
        return core.Stack(name)
    except core.StackError as e:
        sys.exit(str(e))


def worker_state(stack: core.Stack) -> str:
    inst = stack.worker()["instance"]
    if not inst:
        return "no worker running"
    return f"worker {inst['id']} ({inst['type']}) {inst['state']}"


def wait(stack: core.Stack, jobs: dict[str, Path], timeout_min: float) -> int:
    pending = dict(jobs)
    deadline = time.time() + timeout_min * 60
    last_line = ""
    failures = 0
    start = time.time()
    while pending and time.time() < deadline:
        states = []
        for job_id, dest in list(pending.items()):
            st = stack.job_status(USER, job_id, include_legacy=True)
            state = (st or {}).get("state", "queued")
            if state in ("queued", "running"):
                states.append(state)
                continue
            del pending[job_id]
            if state == "done":
                stack.download(st, dest)
                extra = " (ratio wider than 2.5:1 was cropped)" if st.get("ratio_clamped") else ""
                print(f"\n✔ {dest}  {st['size'][0]}x{st['size'][1]}  seed {st['request']['seed']}"
                      f"  {st.get('seconds')}s{extra}")
            else:
                failures += 1
                print(f"\n✘ {job_id}: {st.get('error')}")
        if pending:
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


def cmd_gen(args, stack: core.Stack) -> int:
    try:
        width, height = sizes.resolve(args.preset, args.ratio, args.size)
    except ValueError as e:
        sys.exit(str(e))
    jobs = core.build_jobs(user=USER, prompt=args.prompt, width=width, height=height, count=args.count,
                           seed=args.seed, steps=args.steps, guidance=args.guidance)
    started = stack.submit(jobs)
    for job in jobs:
        print(f"queued {job['job_id']}  {width}x{height}  seed {job['seed']}")
    if started:
        print("starting a GPU worker (cold start is a few minutes)")
    if args.no_wait:
        return 0
    return wait(stack, {j["job_id"]: args.out / core.filename_for(j) for j in jobs}, args.timeout)


def cmd_fetch(args, stack: core.Stack) -> int:
    st = stack.job_status(USER, args.job_id, include_legacy=True)
    if not st or st["state"] == "queued":
        print("no result yet (still queued)")
        return 1
    if st["state"] != "done":
        print({k: v for k, v in st.items() if k != "_prefix"})
        return 1
    dest = args.out / core.filename_for(st["request"])
    stack.download(st, dest)
    print(dest)
    return 0


def cmd_status(args, stack: core.Stack) -> int:
    q = stack.queue()
    print(f"queue: {q['waiting']} waiting, {q['in_progress']} in progress")
    print(f"asg:   desired {stack.worker()['desired']}, {worker_state(stack)}")
    return 0


def cmd_stop(args, stack: core.Stack) -> int:
    stack.stop(purge=args.purge)
    print("desired capacity set to 0; the worker will be terminated")
    if args.purge:
        print("queue purged")
    return 0


def cmd_logs(args, stack: core.Stack) -> int:
    log = stack.latest_log(args.lines)
    if not log:
        print("no logs yet (uploaded when a worker shuts down; use SSM Session Manager on a live one)")
        return 1
    print(f"== {log[0]} ==\n{log[1]}")
    return 0


def cmd_presets(args) -> int:
    for name, (w, h, desc) in sizes.PRESETS.items():
        print(f"  {name:<14} {w:>5}x{h:<5} {desc}")
    return 0


def cmd_serve(args) -> int:
    try:
        import uvicorn

        from . import web
    except ImportError:
        sys.exit("The web UI needs extra packages: pip install -e 'cli[web]'")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"warning: serving on {args.host} with no login; anyone who can reach it can spend your GPU money")
    app = web.create_app(stack=open_stack(args.stack), auth_mode="none")
    url = f"http://{'localhost' if args.host in ('127.0.0.1', '::1') else args.host}:{args.port}"
    print(f"High Destination UI on {url}  (Ctrl+C to stop)")
    if args.open:
        import webbrowser

        webbrowser.open(url)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hd", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stack", default=core.DEFAULT_STACK, help="CloudFormation stack name")
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

    sv = sub.add_parser("serve", help="run the web UI locally")
    sv.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1, this machine only)")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--open", action="store_true", help="open it in your browser")

    args = p.parse_args(argv)
    if args.cmd == "presets":
        return cmd_presets(args)
    if args.cmd == "serve":
        return cmd_serve(args)
    stack = open_stack(args.stack)
    return {"gen": cmd_gen, "fetch": cmd_fetch, "status": cmd_status, "stop": cmd_stop, "logs": cmd_logs}[args.cmd](
        args, stack
    )


if __name__ == "__main__":
    sys.exit(main())
