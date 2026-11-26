"""The data behind the dashboard, separate from the widgets.

Two refresh speeds keep it cheap: `refresh_runtime` (registry, liveness, hook
activity: every 2 s or on an event) and `refresh_git` (git-derived columns and
conflict predictions: every 10 s, or right after a commit event).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from . import gitutil
from .workspace import AgentView, Workspace, merge_state

GIT_FIELDS = ("ahead", "behind", "files", "dirty", "dirty_paths", "changed_paths", "head_sha", "merged", "last_subject")
STATUS_ORDER = {"needs_input": 0, "setup": 1, "starting": 1, "working": 2, "running": 2, "idle": 3, "finished": 4,
                "crashed": 5, "stopped": 6, "untracked": 7, "deleted": 8}


@dataclass
class Progress:
    step: str
    frac: float
    started: float = field(default_factory=time.monotonic)


class Dashboard:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self.views: dict[str, AgentView] = {}
        self.progress: dict[str, Progress] = {}
        self.default_sha = ""
        self.show_hidden = False
        self.filter = ""
        self._git: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.git_refreshed = 0.0
        self.activity = None  # set by Phase 2 (hook events)
        self.messages = None
        self.conflicts = None

    # -- refresh --------------------------------------------------------------------
    def refresh_runtime(self) -> None:
        views = self.ws.views(show_hidden=self.show_hidden, git=False)
        with self._lock:
            for v in views:
                cached = self._git.get(v.name)
                if cached:
                    for k in GIT_FIELDS:
                        setattr(v, k, cached[k])
            self._apply_activity(views)
            for v in views:
                v.merge_state = merge_state(v) if v.branch else "clean"
            self.views = {v.name: v for v in views}
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
        self._apply_conflicts(git=True)

    def refresh_all(self) -> None:
        self.refresh_runtime()
        self.refresh_git()

    def _apply_activity(self, views: list[AgentView]) -> None:
        if self.activity is not None:
            self.activity.apply(views)

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
                     f in v.task.lower() or any(f in p.lower() for p in v.dirty_paths + v.changed_paths)]
        return sorted(views, key=lambda v: (STATUS_ORDER.get(v.status, 9), v.created_at or "~", v.name))

    def get(self, name: str) -> AgentView | None:
        with self._lock:
            return self.views.get(name)

    def counts(self) -> dict[str, int]:
        with self._lock:
            views = [v for v in self.views.values() if v.status not in ("untracked", "deleted")]
        c = {"agents": len(views),
             "working": sum(v.status in ("working", "running") for v in views),
             "needs_input": sum(v.status == "needs_input" for v in views),
             "conflicts": len(self.conflicts.rows) if self.conflicts is not None else 0}
        return c
