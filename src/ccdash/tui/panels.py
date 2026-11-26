"""Details, Messages and Conflicts panels."""

from __future__ import annotations

import time

from rich.text import Text
from textual.widgets import Static

from ..workspace import AgentView
from .theme import ACCENT, DIM, STATUS_GLYPH, STATUS_STYLE


def _ago(ts: float | None) -> str:
    if not ts:
        return ""
    s = max(0, int(time.time() - ts))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    return f"{s // 3600}h ago"


def kv(t: Text, key: str, value, style: str = "default") -> None:
    t.append(f"{key:<10}", style=DIM)
    if isinstance(value, Text):
        t.append_text(value)
    else:
        t.append(str(value), style=style)
    t.append("\n")


def shimmer(text: str, frame: int, width: int = 4) -> Text:
    """A soft highlight sweeping over `text`: this agent is doing it right now."""
    t = Text(text)
    if not text:
        return t
    pos = frame % (len(text) + width * 2) - width
    for i in range(max(0, pos), min(len(text), pos + width)):
        t.stylize(f"bold {ACCENT}", i, i + 1)
    return t


class DetailsPanel(Static):
    def show(self, v: AgentView | None, *, repo_rel, default_branch: str, default_sha: str, activity=None,
             peek: list[str] | None = None, frame: int = 0, animate: bool = False, owns: list[str] | None = None,
             tests: str = "") -> None:
        if v is None:
            self.update(Text("No agent selected. Press n to start one.", style=DIM))
            return
        t = Text()
        if v.status == "untracked":
            kv(t, "Worktree", repo_rel(v.worktree))
            kv(t, "Branch", v.branch or "(detached)")
            t.append("\nNot started by ccdash. Press A to adopt it.", style=DIM)
            self.update(t)
            return
        kv(t, "Worktree", repo_rel(v.worktree) if v.worktree else "(being created)")
        base = Text.assemble((v.branch, "default"), (f"  (base {v.base_ref} @ {v.base_sha[:7]})", DIM))
        kv(t, "Branch", base)
        if activity is not None and activity.last:
            last = shimmer(activity.last, frame) if animate and v.status == "working" else Text(activity.last)
            last.append(f"  {_ago(activity.last_ts)}", style=DIM)
            kv(t, "Last", last)
        commits = f"{v.ahead} ahead, {v.behind} behind {default_branch}"
        if v.last_subject:
            commits += f' · last "{v.last_subject[:40]}"'
        kv(t, "Commits", commits)
        if v.dirty:
            kv(t, "Dirty", f"{v.dirty} file(s): " + ", ".join(v.dirty_paths[:4]) + (" …" if v.dirty > 4 else ""))
        if owns:
            kv(t, "Owns", "  ".join(owns[:6]))
        if tests:
            kv(t, "Tests", tests)
        if v.setup and v.setup not in ("ok", ""):
            kv(t, "Setup", v.setup, style="yellow" if v.setup.startswith("failed") else ACCENT)
        status = Text.assemble((STATUS_GLYPH.get(v.status, " ") + " ", STATUS_STYLE.get(v.status, "default")),
                               v.status.replace("_", " "))
        if v.exit and not v.alive:
            status.append(f"  (exit {v.exit.get('exitstatus')})", style=DIM)
        kv(t, "Status", status)
        if peek is not None:
            t.append("── live output ──\n", style=DIM)
            for line in peek:
                t.append(line[:200] + "\n", style="default")
        t.no_wrap = True
        t.overflow = "ellipsis"
        self.update(t)


class MessagesPanel(Static):
    def show(self, messages: list | None, highlight: str | None = None, limit: int = 60) -> None:
        if not messages:
            self.update(Text("No messages yet. Agents message each other with SendMessage; press t to talk.",
                             style=DIM))
            return
        t = Text()
        for m in messages[-limit:]:
            t.append_text(m.render(highlight=highlight))
            t.append("\n")
        self.update(t)


class ConflictsPanel(Static):
    def show(self, rows: list | None, flash: set | None = None) -> None:
        if not rows:
            self.update(Text("No conflicts.", style=DIM))
            return
        t = Text()
        for r in rows:
            t.append_text(r.render(flash=bool(flash and r.key in flash)))
            t.append("\n")
        t.rstrip()
        self.update(t)
