"""Pull requests through the `gh` CLI. Everything is optional: without gh (or without a
login) ccdash simply shows no PR state and offers no "open PR"."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import gitutil

_FAILED = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_auth: dict[tuple, bool] = {}


class GhError(Exception):
    pass


@dataclass(frozen=True)
class PullRequest:
    number: int
    state: str  # OPEN | MERGED | CLOSED
    checks: str = ""  # ✓ all passed, ✗ one failed, … still running, "" none
    url: str = ""

    @property
    def open(self) -> bool:
        return self.state == "OPEN"

    @property
    def merged(self) -> bool:
        return self.state == "MERGED"

    @property
    def label(self) -> str:
        return f"PR #{self.number}" + (f" {self.checks}" if self.checks else "")


def gh_command() -> list[str] | None:
    """CCDASH_GH (a path, or a JSON argv: tests use a fake), `gh` on PATH, or the default Windows install."""
    env = os.environ.get("CCDASH_GH")
    if env:
        return json.loads(env) if env.lstrip().startswith("[") else [env]
    found = shutil.which("gh")
    if found:
        return [found]
    if sys.platform == "win32":
        p = Path(os.environ.get("ProgramFiles") or r"C:\Program Files") / "GitHub CLI" / "gh.exe"
        if p.exists():
            return [str(p)]
    return None


def _gh(args: list[str], cwd: Path | str, timeout: float = 30) -> subprocess.CompletedProcess:
    cmd = gh_command()
    if cmd is None:
        raise GhError("gh is not installed")
    try:
        return subprocess.run([*cmd, *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(f"gh {' '.join(args[:2])}: {exc}") from exc


def available(cwd: Path | str) -> bool:
    """gh is installed and logged in. Asked once per process and gh command."""
    cmd = gh_command()
    if cmd is None:
        return False
    key = tuple(cmd)
    if key not in _auth:
        try:
            _auth[key] = _gh(["auth", "status"], cwd, timeout=10).returncode == 0
        except GhError:
            _auth[key] = False
    return _auth[key]


def summarize_checks(rollup: list | None) -> str:
    if not rollup:
        return ""
    results = [str(c.get("conclusion") or c.get("state") or "").upper() for c in rollup]
    if any(r in _FAILED for r in results):
        return "✗"
    running = any(str(c.get("status") or "COMPLETED").upper() != "COMPLETED" for c in rollup)
    if running or any(r in ("", "PENDING", "EXPECTED") for r in results):
        return "…"
    return "✓"


def pr_view(cwd: Path | str, branch: str) -> PullRequest | None:
    try:
        p = _gh(["pr", "view", branch, "--json", "state,number,statusCheckRollup,url"], cwd)
    except GhError:
        return None
    if p.returncode != 0:
        return None  # no pull request for this branch
    try:
        data = json.loads(p.stdout)
        return PullRequest(number=int(data["number"]), state=str(data.get("state") or "").upper(),
                           checks=summarize_checks(data.get("statusCheckRollup")), url=str(data.get("url") or ""))
    except (ValueError, KeyError, TypeError):
        return None


class PRCache:
    """`gh pr view` per branch, at most once a minute: it is a network call."""

    def __init__(self, cwd: Path | str, ttl: float = 60.0):
        self.cwd = cwd
        self.ttl = ttl
        self._items: dict[str, tuple[float, PullRequest | None]] = {}
        self._lock = threading.Lock()

    def lookup(self, branch: str) -> PullRequest | None:
        now = time.monotonic()
        with self._lock:
            hit = self._items.get(branch)
        if hit is not None and now - hit[0] < self.ttl:
            return hit[1]
        pr = pr_view(self.cwd, branch)
        with self._lock:
            self._items[branch] = (now, pr)
        return pr

    def forget(self, branch: str) -> None:
        with self._lock:
            self._items.pop(branch, None)


def create_pr(cwd: Path | str, branch: str, remote: str = "origin") -> str:
    """Push the branch (never forced) and open a pull request for it. Returns the PR's URL."""
    try:
        gitutil.git(["push", "-u", remote, branch], cwd, timeout=120)
    except gitutil.GitError as exc:
        raise GhError(f"git push failed: {exc.stderr}") from exc
    p = _gh(["pr", "create", "--fill", "--head", branch], cwd, timeout=60)
    if p.returncode != 0:
        raise GhError((p.stderr or p.stdout).strip() or "gh pr create failed")
    lines = [x.strip() for x in p.stdout.splitlines() if x.strip()]
    return lines[-1] if lines else ""
