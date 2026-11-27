"""The merge menu's actions: ask the agent to rebase, merge locally, open a pull request,
and the diff an agent's branch would bring.

ccdash never force-pushes, never rewrites the default branch and never merges without a
keypress. A local merge adds one merge commit to the default branch, after the configured
tests passed on the merged tree; if anything fails it is aborted and the branch is left
exactly as it was.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import gitutil, worktree
from .conflicts import file_list
from .repo import has_remote
from .workspace import WorkspaceError


class MergeError(WorkspaceError):
    pass


@dataclass
class MergeResult:
    name: str
    sha: str  # the merge commit on the default branch
    files: list[str]
    notified: list[str] = field(default_factory=list)  # agents that got the "what landed" notice
    tested: bool = False


@dataclass
class Menu:
    title: str
    note: str
    choices: list[tuple[str, str, str]]  # (key, action, label) for modals.ChoiceModal


def rebase_text(default: str) -> str:
    return f"Rebase on {default}, resolve conflicts, run the tests, report back."


def ask_rebase(ws, name: str, dashboard=None) -> str:
    from . import messaging

    res = messaging.notice(ws, name, rebase_text(ws.default_branch), dashboard=dashboard)
    tail = " (queued: it is waiting for a permission answer)" if res == "queued" else ""
    return f"Asked {name} to rebase on {ws.default_branch}{tail}."


def main_checkout_problem(ws) -> str | None:
    """Why a local merge cannot run in the main checkout right now (None: it can)."""
    root = ws.repo.main_root
    default = ws.default_branch
    current = gitutil.out(["symbolic-ref", "--quiet", "--short", "HEAD"], root, check=False)
    if current != default:
        return f"the main checkout is on {current or 'a detached HEAD'}, not on {default}"
    if (ws.repo.common_dir / "MERGE_HEAD").exists():
        return "a merge is already in progress in the main checkout"
    if gitutil.out(["status", "--porcelain", "--untracked-files=no"], root, check=False):
        return "the main checkout has uncommitted changes"
    ref = ws.compare_ref()
    if ref != default:  # agents start from origin's newer copy: merging one here would drag those commits in
        n = gitutil.out(["rev-list", "--count", f"{default}..{ref}"], root, check=False) or "some"
        return f"the main checkout is behind {ref} by {n} commit(s); pull first"
    return None


def _abort(root: Path) -> None:
    gitutil.git(["merge", "--abort"], root, check=False)


def merge_local(ws, name: str, dashboard, progress=None) -> MergeResult:
    """`git merge --no-ff --no-commit`, the configured tests, then the commit. Any failure aborts the merge."""
    progress = progress or (lambda step: None)
    v = dashboard.get(name)
    if v is None or not v.branch:
        raise MergeError(f"no agent named {name!r}")
    if v.merge_state != "ready":
        raise MergeError(f"{name} is not ready to merge (state: {v.merge_state})")
    problem = main_checkout_problem(ws)
    if problem:
        raise MergeError(f"cannot merge locally: {problem}")
    root, default, cfg = ws.repo.main_root, ws.default_branch, ws.cfg
    merge_head = ws.repo.common_dir / "MERGE_HEAD"
    before = gitutil.out(["rev-parse", "HEAD"], root)
    progress("merging")
    p = gitutil.git(["merge", "--no-ff", "--no-commit", v.branch], root, check=False)
    if p.returncode != 0:
        files = gitutil.out(["diff", "--name-only", "--diff-filter=U"], root, check=False).splitlines()
        if merge_head.exists():
            _abort(root)
        if files:
            raise MergeError(f"merging {v.branch} into {default} conflicts in {file_list(files)}; the merge was "
                             f"aborted and {default} is unchanged. Ask the agent to rebase.")
        raise MergeError(f"git merge failed, {default} is unchanged: {(p.stderr or p.stdout).strip()[:300]}")
    if not merge_head.exists():
        raise MergeError(f"{v.branch} is already part of {default}")
    tested = False
    if cfg.test:
        progress("testing")
        log = ws.state.logs_dir / f"{name}.merge-test.log"
        try:
            code = worktree.run_setup(cfg.test, root, log)
        except Exception as exc:  # noqa: BLE001
            _abort(root)
            raise MergeError(f"could not run `{cfg.test}`: {exc}; the merge was aborted") from exc
        if code != 0:
            _abort(root)
            raise MergeError(f"`{cfg.test}` failed (exit {code}) on the merged tree; the merge was aborted and "
                             f"{default} is unchanged. Log: {log}")
        tested = True
    progress("committing")
    c = gitutil.git(["commit", "--no-edit", "--quiet"], root, check=False)
    if c.returncode != 0:
        _abort(root)
        raise MergeError(f"git commit failed; the merge was aborted: {(c.stderr or c.stdout).strip()[:300]}")
    sha = gitutil.out(["rev-parse", "HEAD"], root)
    files = gitutil.out(["diff", "--name-only", before, sha], root, check=False).splitlines()
    dashboard.refresh_git()  # new predictions for every other branch; the tracker tells the other agents
    tracker = dashboard.conflicts
    notified = list(tracker.landings.get(name, [])) if tracker is not None else []
    return MergeResult(name=name, sha=sha, files=files, notified=notified, tested=tested)


def pr_problem(ws) -> str | None:
    from . import github

    if not has_remote(ws.repo):
        return "this repository has no remote named origin"
    if github.gh_command() is None:
        return "gh is not installed"
    if not github.available(ws.repo.main_root):
        return "gh is not logged in (run `gh auth login`)"
    return None


def open_pr(ws, name: str, dashboard=None) -> str:
    """`git push -u origin <branch>` (never forced), then `gh pr create --fill`. Returns the URL."""
    from . import github

    rec = ws.record(name)
    problem = pr_problem(ws)
    if problem:
        raise MergeError(f"cannot open a pull request: {problem}")
    try:
        url = github.create_pr(ws.repo.main_root, rec.branch)
    except github.GhError as exc:
        raise MergeError(f"could not open a pull request for {rec.branch}: {exc}") from exc
    tracker = getattr(dashboard, "conflicts", None)
    if tracker is not None and tracker.prs is not None:
        tracker.prs.forget(rec.branch)
    return url


_WHY_NOT = {"conflict": "it conflicts with {default}", "dirty": "its worktree has uncommitted changes",
            "committed": "the agent is still working", "clean": "there is nothing to merge"}


def menu(ws, v, dashboard) -> Menu:
    """What the merge menu offers for one agent, and why something is missing."""
    default, cfg = ws.default_branch, ws.cfg
    tracker = getattr(dashboard, "conflicts", None)
    pr = tracker.pull_request(v.name) if tracker is not None else None
    state = v.merge_state
    choices: list[tuple[str, str, str]] = []
    notes = [f"{v.branch} · +{v.ahead} −{v.behind} {default} · {state}"]
    if state == "merged":
        choices.append(("c", "cleanup", "delete the worktree and the branch (it is merged)"))
        return Menu(f"Merge {v.name}", "\n".join(notes), choices)
    if state == "conflict" and tracker is not None:
        notes.append(f"Conflicts with {default} in {file_list(tracker.conflict_files(v.name), 4)}.")
    if v.alive:
        hint = " (best for conflicts: it knows its changes)" if state == "conflict" else ""
        choices.append(("r", "rebase", f"ask {v.name} to rebase on {default}{hint}"))
    else:
        notes.append(f"{v.name} is not running; restart it (r) to have it rebase.")
    if state == "ready":
        problem = main_checkout_problem(ws)
        if problem is None:
            then = f", if `{cfg.test}` passes" if cfg.test else ""
            choices.append(("l", "local", f"merge into {default} locally{then}"))
        else:
            notes.append(f"No local merge: {problem}.")
    elif pr is not None and pr.open:
        notes.append(f"{pr.label} is open: {pr.url}")
    else:
        notes.append("No local merge: " + _WHY_NOT.get(state, f"the state is {state}").format(default=default) + ".")
    if v.ahead and not (pr is not None and pr.open) and pr_problem(ws) is None:
        choices.append(("p", "pr", "push the branch and open a pull request"))
    return Menu(f"Merge {v.name}", "\n".join(notes), choices)


# -- diff -----------------------------------------------------------------------------------
def has_diff(ws, branch: str) -> bool:
    return gitutil.git(["diff", "--quiet", f"{ws.compare_ref()}...{branch}"], ws.repo.main_root,
                       check=False).returncode == 1


def _has_less(cwd: Path) -> bool:
    """`less` on PATH, or the one Git for Windows ships (git finds that one by itself)."""
    if shutil.which("less"):
        return True
    exec_path = gitutil.out(["--exec-path"], cwd, check=False)  # <git>/mingw64/libexec/git-core
    parents = Path(exec_path).parents if exec_path else []
    return len(parents) > 2 and (parents[2] / "usr" / "bin" / "less.exe").exists()


def diff_command(ws, branch: str) -> tuple[list[str], dict]:
    """argv and environment for `git diff <default>...<branch>` in a pager that waits for q."""
    env = {**os.environ, "LESS": "-R"}  # git's default is -FRX: a short diff would flash by and be gone
    args = ["git"]
    color = []
    if sys.platform == "win32":
        if _has_less(ws.repo.main_root):
            args += ["-c", "core.pager=less"]
        else:
            args += ["-c", "core.pager=more"]
            color = ["--no-color"]  # more does not understand color codes
    return [*args, "--paginate", "diff", *color, f"{ws.compare_ref()}...{branch}"], env
