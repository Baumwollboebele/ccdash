"""Create, populate, set up and remove agent worktrees."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import gitutil
from .config import Config
from .gitignore import PatternSet
from .gitutil import GitError
from .repo import Repo

MAX_PATH = 259


class WorktreeError(Exception):
    pass


@dataclass
class Worktree:
    path: Path
    branch: str
    base_ref: str
    base_sha: str


def worktree_root(repo: Repo, cfg: Config) -> Path:
    p = Path(cfg.worktree_dir)
    return (p if p.is_absolute() else repo.main_root / p).resolve()


def ensure_excluded(repo: Repo, cfg: Config) -> None:
    """Keep the main checkout clean: list the worktree dir in .git/info/exclude (never committed)."""
    root = worktree_root(repo, cfg)
    try:
        rel = root.relative_to(repo.main_root).as_posix()
    except ValueError:
        return  # worktrees live outside the checkout
    line = f"/{rel}/"
    exclude = repo.common_dir / "info" / "exclude"
    try:
        current = exclude.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    if line in current.splitlines():
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if current.endswith("\n") or not current else "\n"
    with open(exclude, "a", encoding="utf-8", newline="\n") as f:
        f.write(f"{sep}# ccdash agent worktrees\n{line}\n")


def needs_longpaths(repo: Repo, cfg: Config, name: str) -> bool:
    """Windows only: would a tracked path exceed MAX_PATH inside the new worktree?"""
    if sys.platform != "win32":
        return False
    if gitutil.out(["config", "--bool", "core.longpaths"], repo.main_root, check=False) == "true":
        return False
    files = gitutil.git(["ls-files", "-z"], repo.main_root).stdout.split("\0")
    longest = max((len(f) for f in files if f), default=0)
    prefix = len(str(worktree_root(repo, cfg) / name)) + 1
    return prefix + longest > MAX_PATH


def _branch_exists(repo: Repo, branch: str) -> bool:
    return gitutil.ok(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], repo.main_root)


def create_worktree(repo: Repo, cfg: Config, name: str, base: str) -> Worktree:
    ensure_excluded(repo, cfg)
    path = worktree_root(repo, cfg) / name
    branch = f"{cfg.branch_prefix}{name}"
    if _branch_exists(repo, branch):
        raise WorktreeError(f"branch {branch} already exists")
    sha = gitutil.git(["rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], repo.main_root, check=False)
    if sha.returncode != 0:
        raise WorktreeError(f"base {base!r} is not a commit")
    base_sha = sha.stdout.strip()
    if needs_longpaths(repo, cfg, name):
        gitutil.git(["config", "--local", "core.longpaths", "true"], repo.main_root)
    existed = path.exists()
    p = gitutil.git(["worktree", "add", "--quiet", "-b", branch, str(path), base_sha], repo.main_root, check=False)
    if p.returncode != 0:
        _rollback(repo, path, branch, remove_dir=not existed)
        raise WorktreeError(f"git worktree add failed: {p.stderr.strip()}")
    return Worktree(path=path.resolve(), branch=branch, base_ref=base, base_sha=base_sha)


def _rollback(repo: Repo, path: Path, branch: str, remove_dir: bool) -> None:
    """Undo a failed `worktree add -b`: git creates the branch even when the checkout fails."""
    if remove_dir and path.exists():
        gitutil.git(["worktree", "remove", "--force", str(path)], repo.main_root, check=False)
        shutil.rmtree(path, ignore_errors=True)
    gitutil.git(["worktree", "prune"], repo.main_root, check=False)
    if _branch_exists(repo, branch):
        gitutil.git(["branch", "-D", branch], repo.main_root, check=False)


def copy_includes(main_root: Path, wt_path: Path) -> list[str]:
    """Copy gitignored files that match .worktreeinclude (same rules as Claude Code)."""
    patterns = PatternSet.from_file(Path(main_root) / ".worktreeinclude")
    if not patterns:
        return []
    listing = gitutil.git(["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"],
                          main_root).stdout.split("\0")
    candidates: list[str] = []
    for entry in filter(None, listing):
        if entry.endswith("/"):
            if patterns.may_reach_into(entry):
                inner = gitutil.git(["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--", entry],
                                    main_root).stdout.split("\0")
                candidates.extend(filter(None, inner))
        else:
            candidates.append(entry)
    copied = []
    for rel in candidates:
        if not patterns.matches(rel):
            continue
        src, dst = Path(main_root) / rel, Path(wt_path) / rel
        if not src.is_file() or dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)
    return sorted(copied)


def run_setup(cmd: str, cwd: Path, log_path: Path, on_line: Callable[[str], None] | None = None,
              timeout: float | None = None) -> int:
    """Run the configured setup command in the worktree; output goes to `log_path`."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"$ {cmd}\n")
        proc = subprocess.Popen(cmd, shell=True, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                creationflags=flags)
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            if on_line:
                on_line(line.rstrip("\r\n"))
        code = proc.wait(timeout=timeout)
        log.write(f"[exit {code}]\n")
    return code


def remove_worktree(repo: Repo, path: Path, branch: str, force: bool = False) -> None:
    path = Path(path)
    if path.exists():
        args = ["worktree", "remove", *(["--force", "--force"] if force else []), str(path)]
        p = gitutil.git(args, repo.main_root, check=False)
        if p.returncode != 0:
            if not force:
                raise WorktreeError(f"git worktree remove failed: {p.stderr.strip()}")
            shutil.rmtree(path, ignore_errors=True)
    gitutil.git(["worktree", "prune"], repo.main_root, check=False)
    if branch and _branch_exists(repo, branch):
        try:
            gitutil.git(["branch", "-D", branch], repo.main_root)
        except GitError as exc:
            raise WorktreeError(str(exc)) from exc


def list_worktrees(repo: Repo) -> list[dict]:
    """`git worktree list --porcelain` as dicts with path, head, branch."""
    out = gitutil.git(["worktree", "list", "--porcelain"], repo.main_root).stdout
    items, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur:
                items.append(cur)
            cur = {}
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur["path"] = Path(os.path.normpath(val)).resolve()
        elif key == "HEAD":
            cur["head"] = val
        elif key == "branch":
            cur["branch"] = val.removeprefix("refs/heads/")
        else:
            cur[key] = val or True
    return items
