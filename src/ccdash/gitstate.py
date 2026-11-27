"""Git-derived state per agent branch: ahead/behind, dirty files, merged, work at risk,
and predicted merge conflicts (`git merge-tree`, cached by SHA pair)."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import gitutil
from .repo import Repo


@dataclass
class BranchState:
    exists: bool = True
    head_sha: str = ""
    ahead: int = 0
    behind: int = 0
    dirty: int = 0
    dirty_paths: list[str] = field(default_factory=list)
    changed_paths: list[str] = field(default_factory=list)  # committed vs merge-base
    files: int = 0  # union of dirty and changed
    last_subject: str = ""
    merged: bool = False


def dirty_paths(wt_path: Path) -> list[str]:
    p = gitutil.git(["status", "--porcelain=v2", "-z", "--untracked-files=all"], wt_path, check=False)
    if p.returncode != 0:
        return []
    paths, items, i = [], p.stdout.split("\0"), 0
    while i < len(items):
        item = items[i]
        i += 1
        if not item:
            continue
        kind = item[0]
        if kind == "1":
            paths.append(item.split(" ", 8)[8])
        elif kind == "2":
            paths.append(item.split(" ", 9)[9])
            i += 1  # the original path follows as its own NUL-terminated entry
        elif kind == "u":
            paths.append(item.split(" ", 10)[10])
        elif kind == "?":
            paths.append(item[2:])
    return paths


def branch_state(repo: Repo, default: str, wt_path: Path | None, branch: str) -> BranchState:
    cwd = repo.main_root
    head = gitutil.git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd, check=False)
    if head.returncode != 0:
        return BranchState(exists=False)
    st = BranchState(head_sha=head.stdout.strip())
    counts = gitutil.out(["rev-list", "--left-right", "--count", f"{default}...{branch}"], cwd, check=False).split()
    if len(counts) == 2:
        st.behind, st.ahead = int(counts[0]), int(counts[1])
    if st.ahead:
        st.changed_paths = [x for x in gitutil.out(["diff", "--name-only", f"{default}...{branch}"], cwd,
                                                    check=False).splitlines() if x]
        st.last_subject = gitutil.out(["log", "-1", "--format=%s", branch], cwd, check=False)
    if wt_path is not None and Path(wt_path).is_dir():
        st.dirty_paths = dirty_paths(Path(wt_path))
        st.dirty = len(st.dirty_paths)
    st.files = len(set(st.changed_paths) | set(st.dirty_paths))
    st.merged = st.ahead == 0 or gitutil.ok(["merge-base", "--is-ancestor", branch, default], cwd)
    return st


@dataclass
class WorkAtRisk:
    uncommitted: int = 0
    unmerged_commits: int = 0
    unpushed_commits: int = 0

    @property
    def empty(self) -> bool:
        return not (self.uncommitted or self.unmerged_commits or self.unpushed_commits)

    def describe(self, default: str) -> list[str]:
        out = []
        if self.uncommitted:
            out.append(f"{self.uncommitted} uncommitted change(s) in the worktree")
        if self.unmerged_commits:
            out.append(f"{self.unmerged_commits} commit(s) not merged into {default}")
        if self.unpushed_commits:
            out.append(f"{self.unpushed_commits} commit(s) not pushed to any remote")
        return out


def work_at_risk(repo: Repo, default: str, wt_path: Path | None, branch: str) -> WorkAtRisk:
    cwd = repo.main_root
    risk = WorkAtRisk()
    if wt_path is not None and Path(wt_path).is_dir():
        risk.uncommitted = len(dirty_paths(Path(wt_path)))
    if not gitutil.ok(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd):
        return risk
    risk.unmerged_commits = int(gitutil.out(["rev-list", "--count", f"{default}..{branch}"], cwd, check=False) or 0)
    if risk.unmerged_commits:
        risk.unpushed_commits = int(gitutil.out(["rev-list", "--count", branch, "--not", "--remotes"], cwd,
                                                check=False) or 0)
    return risk


# -- merge-tree predictions ------------------------------------------------------
@dataclass(frozen=True)
class MergePrediction:
    conflict: bool
    files: tuple[str, ...] = ()
    error: str = ""


_write_tree: bool | None = None


def supports_write_tree(cwd: Path | str) -> bool:
    """`git merge-tree --write-tree` (a real merge, with rename detection) needs git 2.38."""
    global _write_tree
    if _write_tree is None:
        m = re.search(r"(\d+)\.(\d+)", gitutil.out(["version"], cwd, check=False))
        _write_tree = bool(m) and (int(m.group(1)), int(m.group(2))) >= (2, 38)
    return _write_tree


_SECTIONS = frozenset({"merged", "added in remote", "added in both", "added in local", "removed in both",
                       "changed in both", "removed in local", "removed in remote"})
_ENTRY = re.compile(r"^  (base|our|their|result)\s+(\d+) ([0-9a-f]{40,64}) (.*)$")


def legacy_conflicts(out: str) -> list[str]:
    """Conflicting paths in the output of the old `git merge-tree <base> <ours> <theirs>`.

    It lists every path the trivial merge could not settle, with a diff of the merged
    result. A conflict is: both sides changed it and the diff carries conflict markers,
    both added it, or one side deleted what the other changed. No rename detection."""
    files: list[str] = []
    kind: str | None = None
    entries: dict[str, tuple[str, str]] = {}
    marked = in_entries = False

    def close() -> None:
        if kind is None or not entries:
            return
        path = next(iter(entries.values()))[1]
        if kind == "changed in both":
            hit = marked
        elif kind in ("added in both", "removed in local"):  # removed here, changed there
            hit = True
        elif kind == "removed in remote":  # removed there: a conflict only if we changed it
            hit = entries.get("our", ("", ""))[0] != entries.get("base", ("", ""))[0]
        else:
            hit = False
        if hit and path not in files:
            files.append(path)

    for line in out.splitlines():
        if line in _SECTIONS:
            close()
            kind, entries, marked, in_entries = line, {}, False, True
            continue
        if kind is None:
            continue
        if in_entries:
            m = _ENTRY.match(line)
            if m:
                entries[m.group(1)] = (m.group(3), m.group(4))
                continue
            in_entries = False
        if line.startswith("+<<<<<<< .our"):
            marked = True
    close()
    return files


class MergeTreeCache:
    """Would merging two commits conflict, and in which files? Asked of `git merge-tree`
    (never touches a worktree or the index) and kept by SHA pair."""

    def __init__(self, repo: Repo):
        self.repo = repo
        self._cache: dict[tuple[str, str], MergePrediction] = {}
        self._lock = threading.Lock()

    def predict(self, ours_sha: str, theirs_sha: str) -> MergePrediction:
        key = (ours_sha, theirs_sha)
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit
        root = self.repo.main_root
        res = self._write_tree(ours_sha, theirs_sha) if supports_write_tree(root) else self._legacy(ours_sha, theirs_sha)
        with self._lock:
            self._cache[key] = res
        return res

    def _write_tree(self, ours: str, theirs: str) -> MergePrediction:
        p = gitutil.git(["merge-tree", "--write-tree", "--name-only", "--no-messages", ours, theirs],
                        self.repo.main_root, check=False)
        if p.returncode == 0:
            return MergePrediction(conflict=False)
        if p.returncode == 1:
            lines = p.stdout.splitlines()[1:]  # first line is the tree id
            return MergePrediction(conflict=True, files=tuple(x for x in lines if x))
        return MergePrediction(conflict=False, error=p.stderr.strip())

    def _legacy(self, ours: str, theirs: str) -> MergePrediction:
        """git older than 2.38: the three-way `merge-tree <base> <ours> <theirs>`."""
        root = self.repo.main_root
        base = gitutil.git(["merge-base", ours, theirs], root, check=False)
        if base.returncode != 0 or not base.stdout.strip():
            return MergePrediction(conflict=False, error="no common ancestor")
        p = gitutil.git(["merge-tree", base.stdout.split()[0], ours, theirs], root, check=False)
        if p.returncode != 0:
            return MergePrediction(conflict=False, error=p.stderr.strip())
        files = legacy_conflicts(p.stdout)
        return MergePrediction(conflict=bool(files), files=tuple(files))
