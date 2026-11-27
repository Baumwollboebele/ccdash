"""Guard mode (`guard = "warn" | "block"`, off by default): before an agent edits a file,
check the path against what other agents claim or are editing.

  warn   The edit goes through. Claude gets a note next to the tool result naming the
         owner and telling it to message that agent. No permission decision is returned:
         "allow" would skip the user's own permission prompt for exactly these edits.
  block  The edit is denied; Claude sees the same explanation as the reason. An agent that
         has already changed the file itself is only warned: two agents that both hold a
         file must not lock each other out of it.

It runs where the PreToolUse hook runs: inside the agent's host (HTTP hooks on Windows) or
in the hook process (command hooks). Stdlib only, and it must never break a tool call:
any error means "no decision".

The mode comes from <state>/guard.json, which every `cc` writes from the config, so a
change reaches running agents. With command hooks, only agents started while guard was
on have the synchronous PreToolUse hook that a decision needs.

Who holds a path:
  * <state>/ownership.json, kept by the dashboard: each agent's claims (announced with a
    `Files:` line, or set by the user) and the files its branch or worktree changes;
  * in a host, also the events since: edits that happened (PostToolUse, so a denied
    attempt holds nothing) and `Files:` announcements, of agents that are running.
"""

from __future__ import annotations

import json
import os
import threading

from .activity import _files_line
from .gitignore import PatternSet
from .hook import EDIT_TOOLS

MODES = ("warn", "block")
TAIL_START = 4 * 1024 * 1024  # a host reads at most the last 4 MB of older events when it starts


class _Json:
    """A JSON file, read again only when it changed."""

    def __init__(self, path: str):
        self.path = path
        self.sig: tuple | None = None
        self.data = None

    def get(self):
        try:
            st = os.stat(self.path)
        except OSError:
            self.sig, self.data = None, None
            return None
        sig = (st.st_mtime_ns, st.st_size)
        if sig != self.sig:
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except (OSError, ValueError):
                self.data = None
            self.sig = sig
        return self.data


def _outside(path: str) -> bool:
    return path.startswith(("/", "..")) or (len(path) > 1 and path[1] == ":")


class Guard:
    def __init__(self, state_dir, agent: str, tail: bool = False):
        self.state_dir = str(state_dir)
        self.agent = agent
        self.tail = tail  # a long-lived host follows events.jsonl; a hook process only reads ownership.json
        self._mode = _Json(os.path.join(self.state_dir, "guard.json"))
        self._ownership = _Json(os.path.join(self.state_dir, "ownership.json"))
        self._agents = _Json(os.path.join(self.state_dir, "agents.json"))
        self._patterns: dict[str, PatternSet] = {}
        self._offset: int | None = None
        self._touched: dict[str, set[str]] = {}
        self._claimed: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def mode(self) -> str:
        data = self._mode.get()
        mode = data.get("mode") if isinstance(data, dict) else None
        return mode if mode in MODES else "off"

    def _lifecycles(self) -> dict[str, str]:
        data = self._agents.get()
        items = data.get("agents") if isinstance(data, dict) else None
        return {a.get("name"): a.get("lifecycle", "") for a in (items or []) if isinstance(a, dict)}

    def _matches(self, glob: str, path: str) -> bool:
        ps = self._patterns.get(glob)
        if ps is None:
            ps = self._patterns[glob] = PatternSet([glob])
        return ps.matches(path)

    def _follow(self) -> None:
        """Take in the events appended since the last call."""
        path = os.path.join(self.state_dir, "events.jsonl")
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if self._offset is None:
            self._offset = max(0, size - TAIL_START)
        if size < self._offset:  # truncated or replaced
            self._offset = 0
            self._touched.clear()
            self._claimed.clear()
        if size == self._offset:
            return
        with open(path, "rb") as f:
            f.seek(self._offset)
            chunk = f.read(size - self._offset)
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        self._offset += end + 1
        for raw in chunk[: end + 1].splitlines():
            if b"PostToolUse" not in raw and b"SendMessage" not in raw:
                continue
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            agent, event, tool = line.get("agent"), line.get("event"), line.get("tool")
            if not agent or not isinstance(agent, str):
                continue
            if event == "PostToolUse" and tool in EDIT_TOOLS and line.get("path"):
                self._touched.setdefault(agent, set()).add(line["path"])
            elif event == "PreToolUse" and tool == "SendMessage" and agent not in self._claimed:
                files = _files_line((line.get("msg") or {}).get("text", ""))
                if files:
                    self._claimed[agent] = files

    def _why(self, path: str, claimed, touched) -> str | None:
        for glob in claimed or ():
            if isinstance(glob, str) and glob and self._matches(glob, path):
                return f"claims {glob}"
        return "is editing it" if path in (touched or ()) else None

    def owners(self, path: str) -> list[tuple[str, str]]:
        """The other agents that hold `path`, each with the reason: ("csv-export", "claims src/ui/**")."""
        if self.tail:
            self._follow()
        life = self._lifecycles()
        data = self._ownership.get()
        agents = data.get("agents") if isinstance(data, dict) else None
        found: dict[str, str] = {}
        for name, entry in (agents if isinstance(agents, dict) else {}).items():
            if name == self.agent or life.get(name) == "deleted" or not isinstance(entry, dict):
                continue
            why = self._why(path, entry.get("claimed"), entry.get("touched"))
            if why:
                found[name] = why
        for name in set(self._touched) | set(self._claimed):
            if name == self.agent or name in found or life.get(name, "running") != "running":
                continue
            why = self._why(path, self._claimed.get(name), self._touched.get(name))
            if why:
                found[name] = why
        return sorted(found.items())

    def _mine(self, path: str) -> bool:
        """Has this agent itself already changed `path`?"""
        data = self._ownership.get()
        agents = data.get("agents") if isinstance(data, dict) else None
        entry = agents.get(self.agent) if isinstance(agents, dict) else None
        if isinstance(entry, dict) and path in (entry.get("touched") or ()):
            return True
        return path in self._touched.get(self.agent, ())

    def decide(self, line: dict) -> dict | None:
        """The hook output for one PreToolUse event line, or None for "nothing to say"."""
        try:
            mode = self.mode()
            path = line.get("path")
            if mode == "off" or line.get("tool") not in EDIT_TOOLS or not path or _outside(path):
                return None
            with self._lock:
                owners = self.owners(path)
                mine = self._mine(path)
            if not owners:
                return None
            names = ", ".join(n for n, _ in owners)
            detail = "; ".join(f"{n} {why}" for n, why in owners)
            if mode == "block" and not mine:  # an agent that already changed the file is warned, not locked out
                return {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason":
                        f"ccdash guard: {path} belongs to another agent ({detail}). Do not edit it. Message {names} "
                        f"with SendMessage, agree on who changes it, and work on something else until you have an "
                        f"answer."}}
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext":
                    f"ccdash guard: {path} is not only yours ({detail}). Message {names} with SendMessage now and "
                    f"agree on this file before you change it again."}}
        except Exception:  # noqa: BLE001  (a guard that fails must not fail the agent's tool call)
            return None


def decider_for(state_dir, name: str):
    """For the host's hook endpoint: (PreToolUse event line) -> hook output or None."""
    return Guard(state_dir, name, tail=True).decide
