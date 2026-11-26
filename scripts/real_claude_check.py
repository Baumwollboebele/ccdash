"""Manual check against the REAL claude CLI (uses your login and quota; Haiku by default).

    python scripts/real_claude_check.py <path to a repo you have already trusted in Claude Code>

Starts two agents through ccdash, waits for them to finish a tiny task (they should
announce their scope to each other with SendMessage), then checks hooks, status,
messages, stop, resume and delete. Leaves the repo as it found it except for
.git/ccdash state. Never run in CI.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ccdash.dashboard import Dashboard  # noqa: E402
from ccdash.workspace import Workspace  # noqa: E402

MODEL = "haiku"
TASK = ("Create a file named notes-{name}.txt in the worktree root containing exactly one line: '{name} was here'. "
        "Do not commit and do not run shell commands. Keep it short.")


def say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait(pred, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = pred()
        if r:
            return r
        time.sleep(0.5)
    raise SystemExit(f"FAILED: timed out waiting for {what}")


def main() -> None:
    repo = Path(sys.argv[1]).resolve()
    ws = Workspace.open(repo)
    d = Dashboard(ws)
    names = ["rc-alpha", "rc-beta"]
    for n in names:
        if ws.registry.get(n) and ws.registry.get(n).lifecycle != "deleted":
            raise SystemExit(f"{n} already exists in {repo}; delete it first (cc rm {n})")
    t0 = time.monotonic()
    for n in names:
        rec = ws.new_agent(TASK.format(name=n), name=n, model=MODEL, permission_mode="acceptEdits")
        say(f"started {n} in {time.monotonic() - t0:.1f}s · session {rec.claude_session_id}")
    seen: dict[str, str] = {}

    def done() -> bool:
        d.refresh_runtime()
        for n in names:
            v = d.get(n)
            if v and seen.get(n) != v.status:
                seen[n] = v.status
                say(f"{n}: {v.status}  last={v.last!r}")
        return all(d.get(n) and d.get(n).status in ("idle", "finished") and
                   d.activity.get(n) and d.activity.get(n).last_message for n in names)

    wait(done, 240, "both agents to finish")
    ok = True
    for n in names:
        a, rec = d.activity.get(n), ws.registry.get(n)
        same_session = a.session_id == rec.claude_session_id
        ok &= same_session
        say(f"{n}: session id honored={same_session} · touched={sorted(a.touched)} · claimed={a.claimed} · "
            f"final={a.last_message[:80]!r}")
        ok &= (Path(rec.worktree) / f"notes-{n}.txt").exists()
    msgs = [(m.sender, m.to, m.delivered) for m in d.messages.items]
    say(f"messages: {msgs}")
    ok &= any(s in names for s, _, _ in msgs)

    for n in names:
        ws.stop(n)
    wait(lambda: (d.refresh_runtime() or True) and all(d.get(n).status == "stopped" for n in names), 30, "stopped")
    say("both stopped (SessionEnd seen: " + str([d.activity.get(n).ended for n in names]) + ")")

    sid = ws.registry.get("rc-alpha").claude_session_id
    ws.restart("rc-alpha")
    wait(lambda: (d.refresh_runtime() or True) and d.activity.get("rc-alpha").session_started_t > time.time() - 60
         and d.activity.get("rc-alpha").session_id == sid and d.get("rc-alpha").status == "idle", 60, "resume")
    say(f"rc-alpha resumed the same session {sid}")
    ws.stop("rc-alpha")

    for n in names:
        ws.delete(n, confirm=n)
    say("deleted both (worktrees and branches removed)")
    print("RESULT:", "OK" if ok else "CHECK THE LOG ABOVE", flush=True)


if __name__ == "__main__":
    main()
