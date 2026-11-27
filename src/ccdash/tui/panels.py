"""Details, Messages and Conflicts panels."""

from __future__ import annotations

import textwrap
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
             tests: str = "", conflict: tuple = (), pr=None) -> None:
        if v is None:
            self.last_text = Text("No agents yet. Press l and say what you want built: the lead starts the agents.\n"
                                  "Or press n to start one agent yourself.", style=DIM)
            self.update(self.last_text)
            return
        t = Text()
        if v.role == "lead":
            kv(t, "Role", "coordinator: turns requests into agents")
            kv(t, "Works in", "the main checkout, no branch of its own")
            if activity is not None and activity.last:
                last = shimmer(activity.last, frame) if animate and v.status == "working" else Text(activity.last)
                last.append(f"  {_ago(activity.last_ts)}", style=DIM)
                kv(t, "Last", last)
            status = Text.assemble((STATUS_GLYPH.get(v.status, " ") + " ", STATUS_STYLE.get(v.status, "default")),
                                   v.status.replace("_", " "))
            kv(t, "Status", status)
            if v.status == "needs_input":
                t.append("Waiting for you: attach (a) to answer. What you tell it now is queued.\n", style="yellow")
            elif not v.alive:
                t.append("Not running. l starts it again with what you tell it; r resumes it as it was.\n", style=DIM)
            else:
                t.append("l talks to it · a attaches · p shows its screen\n", style=DIM)
            self._tail(t, activity, peek)
            self.last_text = t
            self.update(t)
            return
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
        if v.merge_state == "conflict" and conflict:
            kv(t, "Conflict", f"with {default_branch} in " + ", ".join(conflict[:4]) + (" …" if len(conflict) > 4 else ""),
               style="red")
        if pr is not None:
            kv(t, "PR", Text.assemble((f"#{pr.number} {pr.state.lower()}", "cyan"),
                                      (f" {pr.checks}" if pr.checks else ""), (f"  {pr.url}", DIM)))
        if owns:
            kv(t, "Owns", "  ".join(owns[:6]) + (f"  +{len(owns) - 6}" if len(owns) > 6 else ""))
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
        self._tail(t, activity, peek)
        self.last_text = t
        self.update(t)

    def _tail(self, t: Text, activity, peek: list[str] | None) -> None:
        """What fills the rest of the panel: the agent's screen while peek is on, otherwise what it
        said last, so that an answer can be read without attaching."""
        room = max(3, (self.size.height or 20) - len(t.plain.splitlines()) - 1)
        if peek is not None:
            t.append("── live output ──\n", style=DIM)
            for line in (peek or ["(no output yet)"])[-room:]:
                t.append(line[:200] + "\n", style="default")
            return
        said = (getattr(activity, "last_message", "") or "").strip()
        if not said:
            return
        width = max(20, (self.size.width or 56) - 1)
        lines: list[str] = []
        for para in said.splitlines():
            lines += textwrap.wrap(para, width) or [""]
        t.append("── said last ──\n", style=DIM)
        for line in lines[:room]:
            t.append(line + "\n")
        if len(lines) > room:
            t.append("… more: p shows its screen, a attaches", style=DIM)


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
    def show(self, rows: list | None, progress=None) -> None:
        if not rows:
            self.update(Text("No conflicts.", style=DIM))
            return
        t = Text()
        for r in rows:
            p = progress(("conflict", r.key)) if progress else None
            flash = p is not None and p < 0.55
            slide = int((1 - p / 0.35) * 12) if (p is not None and p < 0.35) else 0
            t.append_text(r.render(flash=flash, slide=slide))
            t.append("\n")
        t.rstrip()
        self.update(t)
