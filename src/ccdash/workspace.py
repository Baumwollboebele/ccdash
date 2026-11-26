"""Agent lifecycle for one repository: start, stop, restart, delete, adopt, and the
reconciled view of every agent. Used by both the CLI and the TUI."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Callable

from . import gitstate, gitutil, worktree
from .config import Config, load_config
from .model import AgentRecord
from .naming import is_valid_name, slugify, unique_name
from .prompt import render_prompt
from .repo import Repo, default_branch, discover, fetch_if_stale, resolve_base
from .runner import Runner, agent_env, claude_args, claude_command, get_runner, is_trusted, session_exists
from .state import Registry, State

Progress = Callable[[str, float], None]


class WorkspaceError(Exception):
    pass


class NotTrusted(WorkspaceError):
    pass


class UnknownAgent(WorkspaceError):
    pass


@dataclass
class DeleteCheck:
    name: str
    lost: list[str]
    needs_confirmation: bool
    running: bool


class ConfirmationRequired(WorkspaceError):
    def __init__(self, check: DeleteCheck):
        self.check = check
        super().__init__(f"deleting {check.name} would lose: {'; '.join(check.lost)}. "
                         f"Confirm by typing the agent name.")


@dataclass
class AgentView:
    name: str
    task: str = ""
    status: str = "stopped"  # setup|starting|running|working|needs_input|idle|finished|stopped|crashed|untracked
    lifecycle: str = "stopped"
    alive: bool = False
    branch: str = ""
    worktree: str = ""
    base_ref: str = ""
    base_sha: str = ""
    created_at: str = ""
    ahead: int = 0
    behind: int = 0
    files: int = 0
    dirty: int = 0
    dirty_paths: list[str] = field(default_factory=list)
    changed_paths: list[str] = field(default_factory=list)
    head_sha: str = ""
    merged: bool = False
    merge_state: str = "clean"
    last_subject: str = ""
    setup: str = ""
    exit: dict | None = None
    record: AgentRecord | None = None

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in (
            "name", "task", "status", "lifecycle", "alive", "branch", "worktree", "base_ref", "created_at",
            "ahead", "behind", "files", "dirty", "merge_state", "last_subject", "setup")}


IDLE_STATUSES = ("idle", "finished", "stopped", "crashed")


def merge_state(v: AgentView) -> str:
    """STATE column from git (conflict and PR states are layered on top by conflicts.py)."""
    moved = bool(v.head_sha and v.base_sha and not v.head_sha.startswith(v.base_sha))
    if moved and v.merged:
        return "merged"
    if v.dirty:
        return "dirty"
    if v.ahead and v.status in IDLE_STATUSES:
        return "ready"
    if v.ahead:
        return "committed"
    return "clean"


class Workspace:
    def __init__(self, repo: Repo, cfg: Config, runner: Runner | None = None):
        self.repo = repo
        self.cfg = cfg
        self.state = State(repo.state_dir)
        self.state.ensure()
        self.registry = Registry(self.state)
        self.runner = runner or get_runner(cfg, self.state, repo)

    @classmethod
    def open(cls, cwd: Path | str | None = None) -> "Workspace":
        repo = discover(cwd)
        return cls(repo, load_config(repo.main_root))

    @cached_property
    def default_branch(self) -> str:
        return default_branch(self.repo, self.cfg)

    @property
    def worktree_root(self) -> Path:
        return worktree.worktree_root(self.repo, self.cfg)

    # -- lookup ---------------------------------------------------------------------
    def record(self, name: str) -> AgentRecord:
        rec = self.registry.get(name)
        if rec is None or rec.lifecycle == "deleted":
            raise UnknownAgent(f"no agent named {name!r}")
        return rec

    def _taken(self, agents: dict[str, AgentRecord]) -> set[str]:
        taken = set(agents)
        root = self.worktree_root
        if root.is_dir():
            taken |= {p.name for p in root.iterdir()}
        prefix = self.cfg.branch_prefix
        heads = gitutil.out(["for-each-ref", "--format=%(refname:short)", f"refs/heads/{prefix}"],
                            self.repo.main_root, check=False)
        taken |= {h[len(prefix):] for h in heads.splitlines() if h.startswith(prefix)}
        return taken

    def _active_count(self, agents: dict[str, AgentRecord]) -> int:
        return sum(1 for a in agents.values()
                   if a.lifecycle == "starting" or (a.lifecycle == "running" and self.runner.is_alive(a)))

    # -- start ----------------------------------------------------------------------
    def new_agent(self, task: str, name: str | None = None, base: str | None = None, model: str | None = None,
                  permission_mode: str | None = None, progress: Progress | None = None,
                  check_trust: bool = True) -> AgentRecord:
        task = (task or "").strip()
        if not task:
            raise WorkspaceError("a task is required")
        progress = progress or (lambda step, frac: None)
        if check_trust and is_trusted(self.repo.main_root) is False:
            raise NotTrusted(f"Claude Code has not been trusted in {self.repo.main_root} yet. Run `claude` there once "
                             f"and accept the trust dialog, then try again.")
        if name is not None and not is_valid_name(name):
            raise WorkspaceError(f"invalid name {name!r}: use lowercase letters, digits and '-', at most 30 chars")
        with self.registry.edit() as agents:
            if self._active_count(agents) >= self.cfg.max_agents:
                raise WorkspaceError(f"max_agents ({self.cfg.max_agents}) reached; stop an agent first")
            taken = self._taken(agents)
            if name is not None and name in taken:
                raise WorkspaceError(f"name {name!r} is already used by an agent, worktree or branch")
            name = name or unique_name(slugify(task), taken)
            rec = AgentRecord(name=name, task=task, lifecycle="starting", runner=self.runner.kind,
                              model=model or self.cfg.model, permission_mode=permission_mode or self.cfg.permission_mode,
                              setup="running" if self.cfg.setup else "")
            agents[name] = rec
        try:
            progress("worktree", 0.1)
            default = self.default_branch
            if base in (None, "", default):
                fetch_if_stale(self.repo, default)
            base_ref = resolve_base(self.repo, default, base)
            wt = worktree.create_worktree(self.repo, self.cfg, name, base_ref)
            rec = self.registry.update(name, worktree=str(wt.path), branch=wt.branch, base_ref=wt.base_ref,
                                       base_sha=wt.base_sha)
            progress("include", 0.3)
            worktree.copy_includes(self.repo.main_root, wt.path)
            if self.cfg.setup:
                progress("setup", 0.4)
                code = worktree.run_setup(self.cfg.setup, wt.path, self.state.logs_dir / f"{name}.setup.log",
                                          on_line=lambda line: progress("setup", 0.6))
                rec = self.registry.update(name, setup="ok" if code == 0 else f"failed: {code}")
            progress("start", 0.8)
            prompt = self._first_prompt(rec)
            self._start(rec, prompt=prompt)
        except Exception:
            rec = self.registry.get(name)
            if rec is not None and rec.lifecycle == "starting":
                self.registry.update(name, lifecycle="stopped", setup=rec.setup if rec.setup != "running" else "")
            raise
        progress("started", 1.0)
        return self.record(name)

    def _first_prompt(self, rec: AgentRecord) -> str:
        agents = self.registry.load()
        others = [a for a in agents.values()
                  if a.name != rec.name and a.lifecycle in ("running", "starting") and a.branch]
        return render_prompt(rec, repo_name=self.repo.name, default_branch=self.default_branch, others=others,
                             owned=self.claims(), shared=self.cfg.shared)

    def claims(self) -> dict[str, list[str]]:
        """Paths each agent claimed or touched (filled in by the ownership tracker)."""
        import json

        try:
            data = json.loads(self.state.ownership_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v.get("claimed", []) + v.get("touched", [])[:6] for k, v in data.get("agents", {}).items()}

    def hook_settings(self, rec: AgentRecord):
        """Settings passed with --settings (hook registration); see hooks_settings.py."""
        from . import hooks_settings

        return hooks_settings.for_agent(self, rec)

    def _start(self, rec: AgentRecord, prompt: str | None = None, resume: str | None = None) -> None:
        session_id = None if resume else str(uuid.uuid4())
        args = claude_args(rec.name, session_id=session_id, resume=resume, model=rec.model,
                           permission_mode=rec.permission_mode, prompt=prompt)
        info = self.runner.start(rec, claude_command(self.cfg), args, agent_env(self.state, rec.name),
                                 Path(rec.worktree), settings=self.hook_settings(rec))
        self.registry.update(rec.name, lifecycle="running", stop_requested=False,
                             claude_session_id=resume or session_id, session=info.get("pipe") or info.get("session", ""),
                             host_pid=info.get("host_pid"), claude_pid=info.get("claude_pid"))

    # -- stop / restart ---------------------------------------------------------------
    def stop(self, name: str, wait: bool = True, timeout: float = 10) -> bool:
        rec = self.record(name)
        self.registry.update(name, stop_requested=True)
        ok = self.runner.stop(rec, timeout=timeout) if wait else True
        if not wait:
            import threading

            threading.Thread(target=self.runner.stop, args=(rec, timeout), daemon=True).start()
        self.registry.update(name, lifecycle="stopped")
        return ok

    def restart(self, name: str) -> AgentRecord:
        rec = self.record(name)
        if self.runner.is_alive(rec):
            raise WorkspaceError(f"{name} is still running")
        if not Path(rec.worktree).is_dir():
            raise WorkspaceError(f"the worktree of {name} is gone: {rec.worktree}")
        self.registry.update(name, restarts=rec.restarts + 1, stop_requested=False)
        rec = self.record(name)
        if session_exists(Path(rec.worktree), rec.claude_session_id):
            self._start(rec, resume=rec.claude_session_id)
        else:  # no conversation yet: start over with the original prompt
            self._start(rec, prompt=self._first_prompt(rec))
        return self.record(name)

    # -- delete / adopt -----------------------------------------------------------------
    def delete_check(self, name: str) -> DeleteCheck:
        rec = self.record(name)
        risk = gitstate.work_at_risk(self.repo, self.default_branch, Path(rec.worktree) if rec.worktree else None,
                                     rec.branch)
        return DeleteCheck(name=name, lost=risk.describe(self.default_branch),
                           needs_confirmation=bool(risk.uncommitted or risk.unmerged_commits),
                           running=self.runner.is_alive(rec))

    def delete(self, name: str, confirm: str | None = None) -> None:
        check = self.delete_check(name)
        if check.needs_confirmation and confirm != name:
            raise ConfirmationRequired(check)
        rec = self.record(name)
        if check.running:
            self.stop(name)
        if rec.worktree:
            worktree.remove_worktree(self.repo, Path(rec.worktree), rec.branch, force=check.needs_confirmation or
                                     bool(gitstate.dirty_paths(Path(rec.worktree)) if Path(rec.worktree).is_dir() else 0))
        elif rec.branch:
            gitutil.git(["branch", "-D", rec.branch], self.repo.main_root, check=False)
        self.registry.update(name, lifecycle="deleted")
        for f in self.state.run_dir.glob(f"{name}.*"):
            try:
                f.unlink(missing_ok=True)
            except OSError:  # Windows: still open by the exiting host or a reader; start() replaces it anyway
                pass

    def untracked_worktrees(self) -> list[dict]:
        root = self.worktree_root
        known = {Path(a.worktree).resolve() for a in self.registry.load().values()
                 if a.worktree and a.lifecycle != "deleted"}
        out = []
        for wt in worktree.list_worktrees(self.repo):
            p = wt.get("path")
            if p and p.parent == root and p not in known:
                out.append(wt)
        return out

    def adopt(self, name: str) -> AgentRecord:
        for wt in self.untracked_worktrees():
            if wt["path"].name == name:
                branch = wt.get("branch", "")
                base_sha = gitutil.out(["merge-base", self.default_branch, branch or wt.get("head", "HEAD")],
                                       self.repo.main_root, check=False)
                rec = AgentRecord(name=name, task="(adopted worktree)", worktree=str(wt["path"]), branch=branch,
                                  base_ref=self.default_branch, base_sha=base_sha, lifecycle="stopped",
                                  runner=self.runner.kind, adopted=True)
                with self.registry.edit() as agents:
                    if name in agents and agents[name].lifecycle != "deleted":
                        raise WorkspaceError(f"an agent named {name!r} already exists")
                    agents[name] = rec
                return rec
        raise UnknownAgent(f"no untracked worktree named {name!r} under {self.worktree_root}")

    # -- views ------------------------------------------------------------------------------
    def reconcile(self) -> None:
        """Agents whose session is gone become stopped (crash or clean exit is read from the exit record)."""
        agents = self.registry.load()
        stale = [a.name for a in agents.values()
                 if (a.lifecycle == "running" or (a.lifecycle == "starting" and _age(a) > 120))
                 and not self.runner.is_alive(a)]
        if stale:
            with self.registry.edit() as agents:
                for n in stale:
                    if n in agents and agents[n].lifecycle in ("running", "starting"):
                        agents[n].lifecycle = "stopped"

    def runtime_status(self, rec: AgentRecord, alive: bool) -> str:
        if rec.lifecycle == "starting":
            return "setup" if rec.setup == "running" else "starting"
        if alive:
            return "running"
        if rec.stop_requested:
            return "stopped"
        ex = self.runner.exit_info(rec)
        if ex is not None and ex.get("exitstatus") == 0 and not ex.get("killed"):
            return "stopped"
        if ex is None and rec.restarts == 0 and rec.adopted:
            return "stopped"
        if ex is None and rec.claude_pid is None:
            return "stopped"
        return "crashed"

    def views(self, show_hidden: bool = False, git: bool = True) -> list[AgentView]:
        self.reconcile()
        agents = [a for a in self.registry.load().values() if show_hidden or a.lifecycle != "deleted"]
        views = []
        for a in agents:
            alive = a.lifecycle != "deleted" and self.runner.is_alive(a)
            views.append(AgentView(
                name=a.name, task=a.task, lifecycle=a.lifecycle, alive=alive, branch=a.branch, worktree=a.worktree,
                base_ref=a.base_ref, base_sha=a.base_sha, created_at=a.created_at, setup=a.setup, record=a,
                status="deleted" if a.lifecycle == "deleted" else self.runtime_status(a, alive),
                exit=self.runner.exit_info(a)))
        for wt in self.untracked_worktrees():
            views.append(AgentView(name=wt["path"].name, task="untracked worktree (press A to adopt)",
                                   status="untracked", worktree=str(wt["path"]), branch=wt.get("branch", "")))
        if git:
            self.fill_git(views)
        return views

    def fill_git(self, views: list[AgentView]) -> None:
        default = self.default_branch

        def one(v: AgentView) -> None:
            if not v.branch or v.status == "deleted":
                return
            st = gitstate.branch_state(self.repo, default, Path(v.worktree) if v.worktree else None, v.branch)
            v.ahead, v.behind, v.files, v.dirty = st.ahead, st.behind, st.files, st.dirty
            v.dirty_paths, v.changed_paths = st.dirty_paths, st.changed_paths
            v.head_sha, v.merged, v.last_subject = st.head_sha, st.merged, st.last_subject
            v.merge_state = merge_state(v)

        live = [v for v in views if v.branch and v.status != "deleted"]
        if len(live) <= 1:
            for v in live:
                one(v)
            return
        with ThreadPoolExecutor(max_workers=min(8, len(live))) as pool:
            list(pool.map(one, live))

    # -- terminal --------------------------------------------------------------------------
    def peek(self, name: str, lines: int = 30) -> list[str]:
        return self.runner.peek(self.record(name), lines=lines)

    def attach(self, name: str) -> int:
        return self.runner.attach(self.record(name))


def _age(rec: AgentRecord) -> float:
    from datetime import datetime

    try:
        return time.time() - datetime.fromisoformat(rec.created_at).timestamp()
    except ValueError:
        return 0.0
