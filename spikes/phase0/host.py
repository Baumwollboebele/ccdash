"""Phase 0 spike: detached ConPTY host for one agent (prototype of ccdash host).

    python host.py <name> <cwd> <rundir> -- <argv of the program to run>

Spawns the program in a ConPTY (pywinpty), keeps all output in <rundir>/<name>.raw
and a ring buffer, and serves \\\\.\\pipe\\ccdash-p0-<name> with tiny JSON requests:
    {"op": "write", "data": "..."}            -> {"ok": true}
    {"op": "since", "offset": N}              -> {"total": T, "data": "..."}
    {"op": "status"}                          -> {"alive": ..., "child_pid": ..., ...}
    {"op": "resize", "rows": R, "cols": C}
    {"op": "kill"}
Exits a few seconds after the child exits.
"""

import json
import os
import sys
import threading
import time
from multiprocessing.connection import Listener

from winpty import PtyProcess

# Variables a parent Claude Code session exports; a child claude must not inherit them.
SCRUB = {
    "CLAUDECODE",
    "CLAUDE_PID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
}


def main():
    name, cwd, rundir = sys.argv[1:4]
    assert sys.argv[4] == "--"
    argv = sys.argv[5:]
    os.makedirs(rundir, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    env["CCDASH_AGENT"] = name
    env["CCDASH_STATE"] = rundir
    env.setdefault("P0_LOG", os.path.join(rundir, "events.jsonl"))

    proc = PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(35, 120))
    raw_path = os.path.join(rundir, f"{name}.raw")
    buf = []  # list of str chunks; spike keeps everything
    total = [0]
    lock = threading.Lock()
    state = {"exit": None}

    info = {
        "name": name,
        "host_pid": os.getpid(),
        "child_pid": proc.pid,
        "pipe": rf"\\.\pipe\ccdash-p0-{name}",
        "argv": argv,
        "started": time.time(),
    }
    with open(os.path.join(rundir, f"{name}.host.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)

    def reader():
        with open(raw_path, "a", encoding="utf-8", newline="") as raw:
            while True:
                try:
                    data = proc.read(4096)
                except EOFError:
                    break
                if not data:
                    continue
                raw.write(data)
                raw.flush()
                with lock:
                    buf.append(data)
                    total[0] += len(data)
        state["exit"] = proc.exitstatus if not proc.isalive() else None

    threading.Thread(target=reader, daemon=True).start()

    def handle(conn):
        try:
            while True:
                req = json.loads(conn.recv_bytes().decode("utf-8"))
                op = req.get("op")
                if op == "write":
                    proc.write(req["data"])
                    resp = {"ok": True}
                elif op == "since":
                    with lock:
                        joined = "".join(buf)
                    off = max(0, min(int(req.get("offset", 0)), len(joined)))
                    resp = {"total": len(joined), "data": joined[off:]}
                elif op == "status":
                    resp = {"alive": proc.isalive(), "exit": state["exit"], **info}
                elif op == "resize":
                    proc.setwinsize(int(req["rows"]), int(req["cols"]))
                    resp = {"ok": True}
                elif op == "kill":
                    proc.terminate(force=True)
                    resp = {"ok": True}
                else:
                    resp = {"error": f"unknown op {op}"}
                conn.send_bytes(json.dumps(resp).encode("utf-8"))
        except (EOFError, OSError):
            pass
        finally:
            conn.close()

    listener = Listener(info["pipe"], family="AF_PIPE")

    def serve():
        while True:
            try:
                conn = listener.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()

    while proc.isalive():
        time.sleep(0.2)
    time.sleep(1.0)
    with open(os.path.join(rundir, f"{name}.exit.json"), "w", encoding="utf-8") as f:
        json.dump({"exitstatus": proc.exitstatus, "ended": time.time()}, f)
    time.sleep(4.0)
    os._exit(0)


if __name__ == "__main__":
    main()
