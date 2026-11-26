"""Phase 0 spike: drive a host.py over its named pipe.

    python client.py launch <name> <cwd> <rundir> -- <argv...>   start a detached host
    python client.py status <name>
    python client.py send <name> <text>      text may contain {ENTER} {ESC} {CTRLC} {UP} {DOWN} {TAB} {BS}
    python client.py screen <name> [chars]   ANSI-stripped tail of the output
    python client.py wait <name> <regex> [timeout] [offset]   wait for regex in output after offset
    python client.py offset <name>           current output length (to pass to wait)
"""

import json
import os
import re
import subprocess
import sys
import time
from multiprocessing.connection import Client

KEYS = {
    "{ENTER}": "\r",
    "{ESC}": "\x1b",
    "{CTRLC}": "\x03",
    "{UP}": "\x1b[A",
    "{DOWN}": "\x1b[B",
    "{TAB}": "\t",
    "{BS}": "\x7f",
}

ANSI = re.compile(
    r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[PX^_][^\x1b]*\x1b\\"  # DCS etc.
    r"|\x1b[@-Z\\-_]"  # 2-char
)
CURSOR_FWD = re.compile(r"\x1b\[(\d*)C")
CURSOR_COL = re.compile(r"\x1b\[\d*G")  # ConPTY encodes runs of spaces as absolute column moves


def strip(text):
    text = CURSOR_FWD.sub(lambda m: " " * int(m.group(1) or 1), text)
    text = CURSOR_COL.sub(" ", text)
    text = ANSI.sub("", text)
    return text.replace("\r", "")


def call(name, req):
    conn = Client(rf"\\.\pipe\ccdash-p0-{name}", family="AF_PIPE")
    try:
        conn.send_bytes(json.dumps(req).encode("utf-8"))
        return json.loads(conn.recv_bytes().decode("utf-8"))
    finally:
        conn.close()


def main():
    cmd, name, *rest = sys.argv[1:]
    if cmd == "launch":
        cwd, rundir = rest[0], rest[1]
        assert rest[2] == "--"
        argv = rest[3:]
        os.makedirs(rundir, exist_ok=True)
        host = os.path.join(os.path.dirname(os.path.abspath(__file__)), "host.py")
        log = open(os.path.join(rundir, f"{name}.host.log"), "ab")
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        p = subprocess.Popen(
            [sys.executable, host, name, cwd, rundir, "--", *argv],
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
        )
        for _ in range(50):
            try:
                print(json.dumps(call(name, {"op": "status"})))
                return
            except OSError:
                time.sleep(0.1)
        print(f"host pid {p.pid} did not open its pipe", file=sys.stderr)
        sys.exit(1)
    elif cmd == "status":
        print(json.dumps(call(name, {"op": "status"}), indent=1))
    elif cmd == "send":
        text = rest[0]
        for k, v in KEYS.items():
            text = text.replace(k, v)
        print(call(name, {"op": "write", "data": text}))
    elif cmd == "offset":
        print(call(name, {"op": "since", "offset": 10**12})["total"])
    elif cmd == "screen":
        n = int(rest[0]) if rest else 3000
        data = call(name, {"op": "since", "offset": 0})["data"]
        print(strip(data[-n:]))
    elif cmd == "wait":
        pattern = re.compile(rest[0])
        timeout = float(rest[1]) if len(rest) > 1 else 30
        offset = int(rest[2]) if len(rest) > 2 else 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = call(name, {"op": "since", "offset": offset})["data"]
            m = pattern.search(strip(data))
            if m:
                print(f"MATCH after {timeout - (deadline - time.time()):.1f}s: {m.group(0)!r}")
                return
            time.sleep(0.3)
        print("TIMEOUT")
        sys.exit(2)
    else:
        sys.exit(f"unknown command {cmd}")


if __name__ == "__main__":
    main()
