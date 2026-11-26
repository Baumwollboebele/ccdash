"""The data behind the dashboard, separate from the widgets.

Refresh speeds keep it cheap:
  refresh_events   new lines of events.jsonl (on file change, and on every runtime tick)
  refresh_runtime  registry, liveness, hook-derived status   (every 2 s or on an event)
  refresh_git      git-derived columns, conflict predictions (every 10 s, or after a commit)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from . import gitutil
from .activity import ActivityTracker
from .messaging import MessageLog
from .state import EventReader
from .workspace import AgentView, Workspace, merge_state

GIT_FIELDS = ("ahead", "behind", "files", "dirty", "dirty_paths", "changed_paths", "head_sha", "merged", "last_subject")
STATUS_ORDER = {"needs_input": 0, "setup": 1, "starting": 1, "working": 2, "running": 2, "idle": 3, "finished": 4,
                "crashed": 5, "stopping": 6, "stopped": 6, "untracked": 7, "deleted": 8}
PRE_START = ("setup", "starting", "untracked", "deleted")


@dataclass
class Progress:
    step: str
    frac: float
    started: float = field(default_factory=time.monotonic)


@dataclass
class Transition:
    name: str
    old: str
    new: str


class Dashboard:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self.views: dict[str, AgentView] = {}
        self.progress: dict[str, Progress] = {}
        self.default_sha = ""
        self.show_hidden = False
        self.filter = ""
        self.no_hooks_after = 15.0
        self.activity = ActivityTracker()
        self.messages = MessageLog()
        self.conflicts = None  # Phase 4: conflicts.ConflictTracker
        self.reader = EventReader(ws.state.events_path)
        self.git_wanted = False  # a commit happened: refresh git soon
        self.transitions: list[Transition] = []
        self.git_refreshed = 0.0
        self._git: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._events_lock = threading.Lock()
        self._last_status: dict[str, str] = {}

    # -- refresh --------------------------------------------------------------------
    def refresh_events(self) -> int:
        with self._events_lock:
            new = self.reader.read_new()
            if not new:
                return 0
            new.sort(key=lambda e: e.get("t") or 0)
            for line in new:
                self.activity.apply(line)
                self.messages.apply(line)
                if line.get("event") == "PostToolUse" and "commit" in (line.get("cmd") or ""):
                    self.git_wanted = True
                if self.conflicts is not None:
                    self.conflicts.on_event(line)
            return len(new)

    def refresh_runtime(self) -> None:
        self.refresh_events()
        self.activity.check_interrupts()
        views = self.ws.views(show_hidden=self.show_hidden, git=False)
        now = time.time()
        with self._lock:
            for v in views:
                cached = self._git.get(v.name)
                if cached:
                    for k in GIT_FIELDS:
                        setattr(v, k, cached[k])
                self._apply_activity(v, now)
                v.merge_state = merge_state(v) if v.branch else "clean"
            self.views = {v.name: v for v in views}
            for v in views:
                old = self._last_status.get(v.name)
                if old is not None and old != v.status:
                    self.transitions.append(Transition(v.name, old, v.status))
                self._last_status[v.name] = v.status
        self._apply_conflicts()

    def refresh_git(self) -> None:
        with self._lock:
            views = list(self.views.values())
        if not views:
            self.refresh_runtime()
            with self._lock:
                views = list(self.views.values())
        self.ws.fill_git(views)
        with self._lock:
            for v in views:
                self._git[v.name] = {k: getattr(v, k) for k in GIT_FIELDS}
                v.merge_state = merge_state(v) if v.branch else "clean"
        self.default_sha = gitutil.out(["rev-parse", "--short", self.ws.default_branch], self.ws.repo.main_root,
                                       check=False)
        self.git_refreshed = time.monotonic()
        self.git_wanted = False
        self._apply_conflicts(git=True)

    def refresh_all(self) -> None:
        self.refresh_runtime()
        self.refresh_git()

    def take_transitions(self) -> list[Transition]:
        with self._lock:
            out, self.transitions = self.transitions, []
        return out

    def _apply_activity(self, v: AgentView, now: float) -> None:
        """Combine liveness (runner) with what the hooks say."""
        if v.status in PRE_START:
            return
        a = self.activity.get(v.name)
        rec = v.record
        run_start = (rec.started_at or 0.0) if rec else 0.0
        this_run = a is not None and a.last_event_t >= run_start - 1.0
        if a is not None:
            v.last = a.last
        if not v.alive:
            if this_run and a.ended:
                v.status = "stopped"  # SessionEnd seen: a clean exit, whatever the exit code
            return
        if not this_run or not a.status:
            v.status = "starting" if now - run_start < self.no_hooks_after else "running"
            v.hooks_missing = now - run_start >= self.no_hooks_after
            return
        v.status = a.status
        if v.status == "stopped":  # SessionEnd seen, the process is still exiting
            v.status = "stopping"
            return
        v.stale = a.stale(now)

    def _apply_conflicts(self, git: bool = False) -> None:
        if self.conflicts is not None:
            self.conflicts.update(self, git=git)

    # -- queries ------------------------------------------------------------------------
    def sorted_views(self) -> list[AgentView]:
        with self._lock:
            views = list(self.views.values())
        f = self.filter.lower().strip()
        if f:
            views = [v for v in views if f in v.name.lower() or f in v.status.replace("_", " ") or
                     f in v.task.lower() or any(f in p.lower() for p in v.dirty_paths + v.changed_paths)
                     or any(f in p.lower() for p in self._touched(v.name))]
        return sorted(views, key=lambda v: (STATUS_ORDER.get(v.status, 9), v.created_at or "~", v.name))

    def _touched(self, name: str) -> set[str]:
        a = self.activity.get(name)
        return a.touched if a else set()

    def get(self, name: str) -> AgentView | None:
        with self._lock:
            return self.views.get(name)

    def counts(self) -> dict[str, int]:
        with self._lock:
            views = [v for v in self.views.values() if v.status not in ("untracked", "deleted")]
        return {"agents": len(views),
                "working": sum(v.status in ("working", "running", "starting", "setup") for v in views),
                "needs_input": sum(v.status == "needs_input" for v in views),
                "conflicts": len(self.conflicts.rows) if self.conflicts is not None else 0}
