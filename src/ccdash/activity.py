"""Per-agent activity derived from hook events (the mapping itself lives in hook.py)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from . import hook

STALE_AFTER = 600.0  # no event for 10 minutes while working
INTERRUPT_CHECK_AFTER = 3.0  # Esc fires no hook; look at the transcript after 3 s of silence


@dataclass
class AgentActivity:
    name: str
    status: str = ""  # "" until the first event of a session
    last: str = ""  # "Edit src/x.ts", "Bash npm test", "SendMessage → dark-mode"
    last_ts: float = 0.0  # when the last tool/prompt activity happened
    last_event_t: float = 0.0  # any event
    session_id: str | None = None
    transcript: str | None = None
    session_started_t: float = 0.0
    started: bool = False
    ended: bool = False
    touched: set[str] = field(default_factory=set)
    claimed: list[str] = field(default_factory=list)
    last_message: str = ""
    tests: str = ""  # "" | running | passed | failed
    commit_in_turn: bool = False
    commits: int = 0
    events: int = 0
    _interrupt_checked: float = 0.0
    _interrupt_mtime: float = -1.0
    _pending_test: bool = False

    def stale(self, now: float | None = None) -> bool:
        return self.status == "working" and (now or time.time()) - self.last_event_t > STALE_AFTER


class ActivityTracker:
    def __init__(self):
        self.agents: dict[str, AgentActivity] = {}

    def get(self, name: str) -> AgentActivity | None:
        return self.agents.get(name)

    def apply(self, line: dict) -> bool:
        name = line.get("agent")
        event = line.get("event", "")
        if not name or name == "*" or event.startswith("ccdash."):
            return False
        a = self.agents.get(name)
        if a is None:
            a = self.agents[name] = AgentActivity(name=name)
        t = float(line.get("t") or time.time())
        a.events += 1
        a.last_event_t = max(a.last_event_t, t)
        if event == "SessionStart":
            a.started, a.ended = True, False
            a.session_id = line.get("session_id") or a.session_id
            a.transcript = line.get("transcript") or a.transcript
            a.session_started_t = t
        elif line.get("session_id") and not a.session_id:
            a.session_id = line["session_id"]
        a.status = hook.next_status(a.status, line)
        tool, path, cmd = line.get("tool"), line.get("path"), line.get("cmd")
        if event == "PreToolUse" and tool:
            if tool == "SendMessage":
                msg = line.get("msg") or {}
                a.last = f"SendMessage → {msg.get('to', '?')}"
                if not a.claimed:
                    a.claimed = _files_line(msg.get("text", ""))
            elif path:
                a.last = f"{tool} {path}"
            elif cmd:
                a.last = f"{tool} {cmd.splitlines()[0][:80]}"
            else:
                a.last = tool
            a.last_ts = t
            if hook.is_test_command(cmd):
                a.tests, a._pending_test = "running", True
        elif event == "PostToolUse":
            if tool in hook.EDIT_TOOLS and path:  # the edit happened; one that was denied touched nothing
                a.touched.add(path)
            if a._pending_test and hook.is_test_command(cmd):
                a.tests, a._pending_test = "passed", False
            if hook.is_commit_command(cmd):
                a.commit_in_turn = True
                a.commits += 1
        elif event == "PostToolUseFailure":
            if a._pending_test and hook.is_test_command(cmd):
                a.tests, a._pending_test = "failed", False
        elif event == "PermissionRequest":
            detail = path or (cmd.splitlines()[0][:60] if cmd else "")
            a.last = f"{tool or 'tool'} {detail}".strip() + " · asks permission"
            a.last_ts = t
        elif event == "UserPromptSubmit":
            inc = line.get("incoming")
            if inc:
                a.last = f"message from {inc.get('from', '?')}"
                a.last_ts = t
            a.commit_in_turn = False
        elif event == "Stop":
            a.last_message = line.get("text", "")
            if a.status == "idle" and a.commit_in_turn and not a.last_message.rstrip().endswith("?"):
                a.status = "finished"
        elif event == "SessionEnd":
            a.ended = True
        return True

    def check_interrupts(self, now: float | None = None) -> list[str]:
        """Working agents silent for a few seconds: did the user press Esc?"""
        now = now or time.time()
        changed = []
        for a in self.agents.values():
            if a.status != "working" or now - a.last_event_t < INTERRUPT_CHECK_AFTER:
                continue
            try:
                mtime = os.stat(a.transcript).st_mtime if a.transcript else 0.0
            except OSError:
                mtime = 0.0
            if mtime == a._interrupt_mtime and now - a._interrupt_checked < 2.0:
                continue
            a._interrupt_checked, a._interrupt_mtime = now, mtime
            if hook.transcript_interrupted(a.transcript, a.last_event_t):
                a.status = "idle"
                a.last = "interrupted"
                changed.append(a.name)
        return changed


def _files_line(text: str) -> list[str]:
    for line in (text or "").splitlines():
        s = line.strip()
        if s.lower().startswith("files:"):
            return [p.strip(" ,") for p in s[6:].split() if p.strip(" ,")]
    return []
