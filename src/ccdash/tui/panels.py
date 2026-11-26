"""Details, Messages and Conflicts panels."""

from __future__ import annotations

import time

from rich.text import Text
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

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
            self.last_text = Text("No agent selected. Press n to start one.", style=DIM)
            self.update(self.last_text)
            return
        t = Text()
        if v.status == "untracked":
            kv(t, "Worktree", repo_rel(v.worktree))
            kv(t, "Branch", v.branch or "(detached)")
            t.append("\nNot started by ccdash. Press A to adopt it.", style=DIM)
            self.last_text = t
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
        if v.stale:
            status.append("  no event for 10 min", style=DIM)
        kv(t, "Status", status)
        if v.hooks_missing:
            t.append("No hook events from this agent yet. It may be waiting on a dialog (attach with a), or the hooks "
                     "are not set up: see the host log under .git/ccdash/logs.\n", style="yellow")
        if v.status == "needs_input":
            t.append("Waiting for you: attach (a) to answer. Messages you send now are queued.\n", style="yellow")
        if peek is not None:
            t.append("── live output ──\n", style=DIM)
            height = max(3, (self.size.height or 20) - len(t.plain.splitlines()) - 1)
            for line in (peek or ["(no output yet)"])[-height:]:
                t.append(line[:200] + "\n", style="default")
        self.last_text = t
        self.update(t)


class MessagesPanel(OptionList):
    """Newest at the bottom; follows new messages unless you scrolled up. Enter expands one."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.messages: list = []
        self._sig = None

    def show(self, messages: list | None, highlight: str | None = None, limit: int = 200) -> None:
        messages = list(messages or [])[-limit:]
        width = max(20, (self.size.width or 60) - 4)
        sig = (len(messages), highlight, width, tuple((m.delivered, m.queued) for m in messages[-20:]),
               messages[-1].t if messages else None)
        if sig == self._sig:
            return
        self._sig = sig
        follow = self.highlighted is None or self.highlighted >= self.option_count - 1
        self.messages = messages
        self.clear_options()
        if not messages:
            self.add_option(Option(Text("No messages yet. Agents talk with SendMessage; t talks to one.", style=DIM),
                                   disabled=True))
            return
        self.add_options([Option(m.render(highlight=highlight, width=width), id=str(i))
                          for i, m in enumerate(messages)])
        if follow:
            self.highlighted = self.option_count - 1
            self.scroll_end(animate=False)

    def plain(self) -> str:
        return "\n".join(m.render().plain for m in self.messages)

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
