"""Stand-in for the background-session side of the `claude` CLI (tests of runner = "bg").

    claude --bg [flags] "<prompt>"          start a worker; prints `backgrounded · <id> · <name>`
    claude --bg --resume <session id>       wake that session under the same id
    claude agents --json [--all] [--cwd P]  the sessions, as the real CLI lists them
    claude logs <id> | stop <id> | attach <id>
Anything else is the interactive fake (fake_claude.py).

Like the real thing: a session's short id is the start of its session id; `pid` and `status`
are listed only while the worker lives; the worker does not see the caller's CCDASH_*
variables (only PATH is documented to reach a background session), so hooks must carry
their identity as arguments; and nothing can be typed into it.
Sessions are recorded in <CCDASH_FAKE_HOME>/jobs/<id>.json, their output in <id>.log.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
JOBS = os.path.join(os.environ.get("CCDASH_FAKE_HOME") or os.path.join(HERE, ".fake-claude"), "jobs")
HIDDEN = ("CCDASH_AGENT", "CCDASH_STATE", "CCDASH_WORKTREE")


def pid_alive(pid):
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            return bool(k32.GetExitCodeProcess(wintypes.HANDLE(h), ctypes.byref(code))) and code.value == 259
        finally:
            k32.CloseHandle(wintypes.HANDLE(h))
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def job_path(short, ext="json"):
    return os.path.join(JOBS, f"{short}.{ext}")


def load(short):
    try:
        with open(job_path(short), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(short, **changes):
    rec = load(short) or {}
    rec.update(changes)
    tmp = job_path(short, f"{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f)
    for _ in range(50):
        try:
            os.replace(tmp, job_path(short))
            return rec
        except PermissionError:
            time.sleep(0.02)
    return rec


def jobs():
    if not os.path.isdir(JOBS):
        return []
    return [r for r in (load(n[:-5]) for n in sorted(os.listdir(JOBS)) if n.endswith(".json")) if r]


def find(ident):
    for rec in jobs():
        if rec["id"] == ident or rec["sessionId"] == ident or rec["id"].startswith(ident):
            return rec
    return None


def session_parser():
    p = argparse.ArgumentParser(prog="claude")
    p.add_argument("prompt", nargs="?")
    p.add_argument("-n", "--name")
    p.add_argument("--settings")
    p.add_argument("--model")
    p.add_argument("--permission-mode")
    p.add_argument("--allowedTools", "--allowed-tools")
    p.add_argument("--session-id")
    p.add_argument("-r", "--resume")
    p.add_argument("-c", "--continue", dest="cont", action="store_true")
    return p


# -- commands -------------------------------------------------------------------------
def background(argv):
    args, _ = session_parser().parse_known_args(argv)
    os.makedirs(JOBS, exist_ok=True)
    old = find(args.resume) if args.resume else None
    session_id = args.resume or args.session_id or str(uuid.uuid4())
    short = old["id"] if old else session_id[:8]
    if old and pid_alive(old.get("pid")):
        sys.stderr.write(f"note: {short} is already running\n")
        return 1
    name = args.name or (old or {}).get("name") or "session"
    save(short, id=short, sessionId=session_id, name=name, cwd=os.getcwd(), startedAt=int(time.time() * 1000),
         state="working", pid=None, status=None)
    worker_argv = [a for a in argv]
    if not args.resume and not args.session_id:
        worker_argv = ["--session-id", session_id, *worker_argv]
    env = {k: v for k, v in os.environ.items() if k not in HIDDEN}
    env["PYTHONIOENCODING"] = "utf-8"
    flags = 0x08000000 | 0x00000200 if sys.platform == "win32" else 0
    with open(job_path(short, "log"), "ab") as log:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--worker", short, *worker_argv], env=env,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, creationflags=flags,
                         start_new_session=sys.platform != "win32")
    deadline = time.time() + 10
    while time.time() < deadline and not (load(short) or {}).get("pid"):
        time.sleep(0.05)
    sys.stdout.write(f"backgrounded · {short} · {name}\n"
                     f"  claude agents             list sessions\n"
                     f"  claude attach {short}    open in this terminal\n"
                     f"  claude logs {short}      show recent output\n"
                     f"  claude stop {short}      stop this session\n")
    return 0


def worker(short, argv):
    import fake_claude as fc

    args, _ = session_parser().parse_known_args(argv)
    s = fc.Session(args)
    save(short, pid=os.getpid(), state="working", status="busy")
    s.fire("SessionStart", source=s.source, model=args.model or "fake-model", session_title=s.name)
    fc.out(f"fake-claude bg {s.name} · session {s.session_id[:8]} · {s.source}")
    if args.prompt:
        s.turn(args.prompt, fc.load_steps(s.name))
    save(short, state="done", status="idle")
    stop = job_path(short, "stop")
    while not os.path.exists(stop):
        time.sleep(0.1)
    s.fire("SessionEnd", reason="other")
    fc.out("stopped")
    save(short, state="stopped", pid=None, status=None)
    try:
        os.remove(stop)
    except OSError:
        pass
    return 0


def agents(argv):
    if "--json" not in argv:
        sys.stderr.write("fake claude agents: only --json is supported\n")
        return 2
    under = os.path.normcase(os.path.abspath(argv[argv.index("--cwd") + 1])) if "--cwd" in argv else None
    out = []
    for rec in jobs():
        alive = pid_alive(rec.get("pid"))
        if not alive and "--all" not in argv:
            continue
        if under and not os.path.normcase(os.path.abspath(rec["cwd"])).startswith(under):
            continue
        entry = {"id": rec["id"], "cwd": rec["cwd"], "kind": "background", "startedAt": rec["startedAt"],
                 "sessionId": rec["sessionId"], "name": rec["name"], "state": rec["state"]}
        if alive:
            entry["pid"], entry["status"] = rec["pid"], rec.get("status") or "idle"
        elif rec["state"] in ("working", "blocked"):
            entry["state"] = "failed"  # the worker is gone without having stopped
        out.append(entry)
    sys.stdout.write(json.dumps(out))
    return 0


def logs(ident):
    rec = find(ident)
    if rec is None:
        sys.stderr.write(f"no background session {ident}\n")
        return 1
    with open(job_path(rec["id"], "log"), "rb") as f:
        sys.stdout.buffer.write(f.read()[-8000:])
    return 0


def stop(ident):
    rec = find(ident)
    if rec is None:
        sys.stderr.write(f"no background session {ident}\n")
        return 1
    open(job_path(rec["id"], "stop"), "w").close()
    deadline = time.time() + 8
    while time.time() < deadline and pid_alive((load(rec["id"]) or {}).get("pid")):
        time.sleep(0.05)
    sys.stdout.write(f"stopped {rec['id']}\n")
    return 0


def main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if argv[:1] == ["--worker"]:
        return worker(argv[1], argv[2:])
    if "--bg" in argv or "--background" in argv:
        return background([a for a in argv if a not in ("--bg", "--background")])
    if argv[:1] == ["agents"]:
        return agents(argv[1:])
    if argv[:1] == ["logs"]:
        return logs(argv[1])
    if argv[:1] in (["stop"], ["kill"]):
        return stop(argv[1])
    if argv[:1] == ["attach"]:
        sys.stdout.write(f"attached {argv[1]}\n")
        return 0
    import fake_claude

    return fake_claude.main()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
