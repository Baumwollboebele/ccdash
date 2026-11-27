"""Claude Code hook events: the entry point and every Claude-specific rule about them.

Two ways in, one format out:
  * command hooks (SessionStart; every event under tmux): `python -I -S -c ... main()`,
    hook JSON on stdin; agent and state dir from CCDASH_AGENT / CCDASH_STATE.
  * HTTP hooks (Windows): the agent's host calls handle() for each POST.
Either way one JSON line is appended to <state>/events.jsonl under events.lock.

Rules: stdlib only, no stdout (except a guard-mode decision, see main), never raise,
always exit 0, drop the event when the state dir is missing or the lock is busy for
more than ~200 ms. Phase 0 findings behind the mapping are in docs/findings.md.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
PATH_TOOLS = EDIT_TOOLS | {"Read"}
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
WORKING_EVENTS = frozenset({"UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied"})
# Notification types that mean "a dialog is waiting for you". idle_prompt fires 60 s after
# every Stop and only confirms idle, so it is deliberately not here.
NEEDS_INPUT_KINDS = frozenset({"permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"})
RAW_LIMIT = 4000
INTERRUPT_MARK = "[Request interrupted by user"

_INCOMING = re.compile(
    r'<cross-session-message\s+from="(?P<addr>[^"]*)"(?:\s+from-name="(?P<name>[^"]*)")?'
    r'(?:\s+from-mode="(?P<mode>[^"]*)")?[^>]*>\s*(?P<body>.*?)\s*</cross-session-message>', re.S)
_DONE = re.compile(r"(?im)^\s*\**DONE\b|\b(all done|task (is )?complete|implemented and tested|ready for review)\b")
_TEST_CMD = re.compile(r"\b(pytest|npm (run )?test|yarn test|pnpm test|go test|cargo test|jest|vitest|mvn test|"
                       r"gradle test|dotnet test|ctest|tox|bun test|rspec|phpunit)\b")


# -- normalization ------------------------------------------------------------------
def _iso(t: float) -> str:
    lt = time.localtime(t)
    off = -(time.altzone if lt.tm_isdst > 0 else time.timezone)
    sign = "+" if off >= 0 else "-"
    off = abs(off)
    return (time.strftime("%Y-%m-%dT%H:%M:%S", lt) + f".{int(t * 1000) % 1000:03d}"
            f"{sign}{off // 3600:02d}:{off % 3600 // 60:02d}")


def rel_path(path: str, root: str | None) -> str:
    """`path` relative to the worktree root, '/'-separated; unchanged if outside it."""
    if not path:
        return path
    if not root or not os.path.isabs(path):
        return path.replace("\\", "/")
    p, r = os.path.normpath(path), os.path.normpath(root).rstrip("\\/")
    pc, rc = os.path.normcase(p), os.path.normcase(r)
    if pc == rc:
        return "."
    if pc.startswith(rc + os.sep):
        return p[len(r) + 1:].replace("\\", "/")
    return p.replace("\\", "/")


def parse_incoming(prompt: str) -> dict | None:
    """A message from another session arrives as a UserPromptSubmit wrapped in <cross-session-message>."""
    if not prompt or "<cross-session-message" not in prompt:
        return None
    m = _INCOMING.search(prompt)
    if not m:
        return None
    return {"from": m.group("name") or m.group("addr"), "mode": m.group("mode") or "", "text": m.group("body")[:4000]}


def _slim(value, depth: int = 0):
    if isinstance(value, str):
        return value if len(value) <= 200 else value[:200] + f"…(+{len(value) - 200})"
    if isinstance(value, dict) and depth < 4:
        return {k: _slim(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list) and depth < 4:
        return [_slim(v, depth + 1) for v in value[:20]]
    return value


def normalize(event: str, payload: dict, agent: str, worktree: str | None, now: float | None = None) -> dict:
    t = time.time() if now is None else now
    line: dict = {"ts": _iso(t), "t": round(t, 3), "agent": agent, "event": event,
                  "session_id": payload.get("session_id")}
    tool = payload.get("tool_name")
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if tool:
        line["tool"] = tool
        if tool in PATH_TOOLS:
            p = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
            if p:
                line["path"] = rel_path(str(p), worktree)
        elif tool in SHELL_TOOLS:
            line["cmd"] = str(ti.get("command", ""))[:300]
        elif tool == "SendMessage":
            line["msg"] = {"to": ti.get("to") or ti.get("recipient") or "",
                           "summary": str(ti.get("summary") or "")[:200],
                           "text": str(ti.get("message") or ti.get("content") or "")[:4000]}
    if event == "UserPromptSubmit":
        prompt = str(payload.get("prompt") or "")
        inc = parse_incoming(prompt)
        if inc:
            line["incoming"] = inc
        else:
            line["text"] = prompt[:500]
    elif event in ("Stop", "StopFailure", "SubagentStop"):
        line["text"] = str(payload.get("last_assistant_message") or "")[:2000]
        if event == "StopFailure":
            line["kind"] = payload.get("error")
    elif event == "Notification":
        line["kind"] = payload.get("notification_type")
        line["text"] = str(payload.get("message") or "")[:300]
    elif event == "SessionStart":
        line["kind"] = payload.get("source")
        line["transcript"] = payload.get("transcript_path")
        line["model"] = payload.get("model")
    elif event == "SessionEnd":
        line["kind"] = payload.get("reason")
    elif event == "PostToolUseFailure":
        line["error"] = str(payload.get("error") or "")[:300]
        line["interrupt"] = bool(payload.get("is_interrupt"))
    elif event == "PermissionDenied":
        line["reason"] = str(payload.get("reason") or "")[:300]
    if payload.get("permission_mode"):
        line["mode"] = payload["permission_mode"]
    raw = json.dumps(payload, ensure_ascii=False)
    line["raw"] = payload if len(raw) <= RAW_LIMIT else _slim(payload)
    if len(json.dumps(line["raw"], ensure_ascii=False)) > RAW_LIMIT:
        line["raw"] = raw[:RAW_LIMIT]
    return line


# -- status mapping -----------------------------------------------------------------
def is_done_message(text: str | None) -> bool:
    return bool(text) and bool(_DONE.search(text))


def next_status(current: str, line: dict) -> str:
    """Status after one event. Latest event wins; SubagentStart/Stop (Claude's own helpers) are ignored."""
    e = line.get("event")
    if e in WORKING_EVENTS:
        return "working"
    if e == "PermissionRequest":
        return "needs_input"
    if e == "Notification":
        return "needs_input" if line.get("kind") in NEEDS_INPUT_KINDS else current
    if e == "Stop":
        return "finished" if is_done_message(line.get("text")) else "idle"
    if e == "StopFailure":
        return "idle"
    if e == "SessionStart":
        return "idle"
    if e == "SessionEnd":
        return "stopped"
    return current


def is_test_command(cmd: str | None) -> bool:
    return bool(cmd) and bool(_TEST_CMD.search(cmd))


def is_commit_command(cmd: str | None) -> bool:
    return bool(cmd) and "git" in cmd and " commit" in cmd


def transcript_interrupted(path: str | None, after: float) -> bool:
    """Esc interrupts fire no hook; the transcript then ends with '[Request interrupted by user]'."""
    if not path:
        return False
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 16384))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return False
    for raw in reversed(tail):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("type") == "assistant":
            return False
        if entry.get("type") != "user":
            continue
        content = (entry.get("message") or {}).get("content")
        text = content if isinstance(content, str) else " ".join(
            c.get("text", "") for c in (content or []) if isinstance(c, dict))
        if INTERRUPT_MARK not in text:
            return False
        stamp = entry.get("timestamp")
        if stamp:
            from datetime import datetime

            try:
                return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() >= after - 1.0
            except ValueError:
                return True
        return True
    return False


# -- writing ------------------------------------------------------------------------
if sys.platform == "win32":
    import msvcrt

    def _lock(fd: int) -> bool:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def append(state_dir: str, line: dict, budget: float = 0.2) -> bool:
    if not state_dir or not os.path.isdir(state_dir):
        return False
    data = (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")
    lfd = os.open(os.path.join(state_dir, "events.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + budget
        while not _lock(lfd):
            if time.monotonic() > deadline:
                return False
            time.sleep(0.002)
        try:
            fd = os.open(os.path.join(state_dir, "events.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        finally:
            _unlock(lfd)
        return True
    finally:
        os.close(lfd)


def handle(event: str, body: bytes, agent: str, state_dir: str, worktree: str | None) -> dict | None:
    try:
        payload = json.loads(body) if body else {}
    except ValueError:
        payload = {"unparsed": body[:RAW_LIMIT].decode("utf-8", "replace")}
    if not isinstance(payload, dict):
        payload = {"unparsed": str(payload)[:RAW_LIMIT]}
    line = normalize(event, payload, agent, worktree)
    append(state_dir, line)
    return line


def main(argv: list[str] | None = None) -> None:
    """Command-hook entry point: `<event> [agent state_dir worktree]`. Never raises.

    The agent's identity comes from the arguments when given (a `claude --bg` session is not
    documented to inherit our environment), else from CCDASH_AGENT / CCDASH_STATE / CCDASH_WORKTREE.
    Nothing is written to stdout, with one exception: in guard mode a PreToolUse hook prints
    its decision (see guard.py), which is why that hook is registered synchronously then."""
    try:
        argv = sys.argv[1:] if argv is None else argv
        event = argv[0] if argv else "Unknown"
        body = sys.stdin.buffer.read()
        given = (argv[1:4] + ["", "", ""])[:3]
        agent = given[0] or os.environ.get("CCDASH_AGENT")
        state = given[1] or os.environ.get("CCDASH_STATE")
        worktree = given[2] or os.environ.get("CCDASH_WORKTREE")
        if agent and state:
            line = handle(event, body, agent, state, worktree)
            if event == "PreToolUse" and line is not None and os.path.exists(os.path.join(state, "guard.json")):
                from .guard import Guard

                decision = Guard(state, agent).decide(line)
                if decision:
                    sys.stdout.write(json.dumps(decision))
                    sys.stdout.flush()
    except BaseException:  # noqa: BLE001  (a hook must never break or slow the agent)
        pass
