"""Find the repository, its main checkout, the shared state dir and the default branch."""

from __future__ import annotations

import hashlib
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import gitutil
from .config import Config
from .naming import slugify


class RepoError(Exception):
    pass


@dataclass(frozen=True)
class Repo:
    main_root: Path  # main checkout (where `git worktree add` runs)
    common_dir: Path  # shared .git dir of all worktrees
    top: Path  # top of the checkout cc was started in
    name: str
    key: str  # short unique id for pipe/session names
    state_dir: Path

    def rel(self, path: Path | str) -> str:
        p = Path(path)
        try:
            return p.resolve().relative_to(self.main_root).as_posix()
        except ValueError:
            return str(p)


def _fallback_state_dir(main_root: Path) -> Path:
    digest = hashlib.sha1(str(main_root).lower().encode()).hexdigest()[:12]
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ccdash" / "state"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "ccdash"
    return base / digest


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / f".probe-{os.getpid()}"
        probe.write_text("x")
        probe.unlink()
        return True
    except OSError:
        return False


def discover(cwd: Path | str | None = None) -> Repo:
    cwd = Path(cwd or os.getcwd())
    p = gitutil.git(["rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel"], cwd, check=False)
    if p.returncode != 0:
        raise RepoError(f"not inside a git repository: {cwd}")
    lines = p.stdout.strip().splitlines()
    if len(lines) < 2:
        raise RepoError(f"{cwd} is inside a bare repository; run cc in a checkout")
    common = Path(lines[0]).resolve()
    top = Path(lines[1]).resolve()
    main_root = common.parent if common.name == ".git" else top
    name = main_root.name
    key = f"{slugify(name, words=3, max_len=20)}-{hashlib.sha1(str(main_root).lower().encode()).hexdigest()[:6]}"
    state = common / "ccdash"
    if not _writable(state):
        state = _fallback_state_dir(main_root)
        state.mkdir(parents=True, exist_ok=True)
    return Repo(main_root=main_root, common_dir=common, top=top, name=name, key=key, state_dir=state)


def default_branch(repo: Repo, cfg: Config) -> str:
    if cfg.default_branch:
        return cfg.default_branch
    head = gitutil.out(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], repo.main_root, check=False)
    if head.startswith("origin/"):
        return head[len("origin/"):]
    for cand in ("main", "master"):
        if gitutil.ok(["show-ref", "--verify", "--quiet", f"refs/heads/{cand}"], repo.main_root):
            return cand
    cur = gitutil.out(["symbolic-ref", "--quiet", "--short", "HEAD"], repo.main_root, check=False)
    return cur or "main"


def has_remote(repo: Repo, remote: str = "origin") -> bool:
    return remote in gitutil.out(["remote"], repo.main_root, check=False).split()


def fetch_if_stale(repo: Repo, branch: str, max_age: float = 24 * 3600, timeout: float = 5) -> None:
    """Fetch the default branch from origin when the last fetch is older than `max_age`."""
    if not has_remote(repo):
        return
    fh = repo.common_dir / "FETCH_HEAD"
    try:
        if time.time() - fh.stat().st_mtime < max_age:
            return
    except OSError:
        pass
    try:
        gitutil.git(["fetch", "--quiet", "origin", branch], repo.main_root, check=False, timeout=timeout)
    except Exception:  # noqa: BLE001  (offline or slow: use what we have)
        pass


def compare_ref(repo: Repo, default: str) -> str:
    """The default branch as agents see it: the local branch, unless origin's copy is strictly
    ahead of it (then origin's). New agents start from this ref, and agent branches are
    measured against it (ahead/behind, changed files, merged, predicted conflicts). Measured
    against a local branch that is behind origin, a fresh agent would seem to have made
    every upstream commit itself."""
    local_ref, remote_ref = f"refs/heads/{default}", f"refs/remotes/origin/{default}"
    out = gitutil.out(["for-each-ref", "--format=%(refname) %(objectname)", local_ref, remote_ref], repo.main_root,
                      check=False)
    refs = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
    local, remote = refs.get(local_ref), refs.get(remote_ref)
    if local and remote:
        if local != remote and gitutil.ok(["merge-base", "--is-ancestor", local, remote], repo.main_root):
            return f"origin/{default}"
        return default
    if local:
        return default
    if remote:
        return f"origin/{default}"
    return "HEAD"


def resolve_base(repo: Repo, default: str, base: str | None) -> str:
    """The ref a new agent branches from: `base` if given (`HEAD` means the main checkout's
    HEAD), otherwise the default branch as compare_ref sees it, so agents start from work
    you merged locally as well as from what landed upstream."""
    if base and base != default:
        return base
    return compare_ref(repo, default)
