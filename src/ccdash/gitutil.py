"""Thin wrapper around the git CLI."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    def __init__(self, args, returncode, stderr):
        self.args_ = args
        self.returncode = returncode
        self.stderr = stderr.strip()
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {self.stderr}")


def git(args: list[str], cwd: Path | str, check: bool = True, timeout: float = 60,
        input: str | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(
        ["git", "-c", "core.quotepath=off", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        input=input,
        stdin=None if input is not None else subprocess.DEVNULL,
    )
    if check and p.returncode != 0:
        raise GitError(args, p.returncode, p.stderr)
    return p


def out(args: list[str], cwd: Path | str, check: bool = True, timeout: float = 60) -> str:
    return git(args, cwd, check=check, timeout=timeout).stdout.strip()


def ok(args: list[str], cwd: Path | str, timeout: float = 60) -> bool:
    return git(args, cwd, check=False, timeout=timeout).returncode == 0
