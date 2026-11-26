"""Shared fixtures: isolated git config, a temp repo with a bare origin, the fake
claude, and cleanup of every agent host a test started."""

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FAKE_CLAUDE = HERE / "fake_claude.py"
WINDOWS = sys.platform == "win32"


@pytest.fixture(autouse=True)
def _watchdog():
    """A hanging test dumps all stacks and ends the run instead of blocking forever."""
    import faulthandler

    faulthandler.dump_traceback_later(int(os.environ.get("CCDASH_TEST_TIMEOUT", "120")), exit=True)
    yield
    faulthandler.cancel_dump_traceback_later()


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run `async def` tests (Textual Pilot) with asyncio.run; no async plugin needed."""
    import asyncio
    import inspect

    if inspect.iscoroutinefunction(pyfuncitem.obj):
        kwargs = {a: pyfuncitem.funcargs[a] for a in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(pyfuncitem.obj(**kwargs))
        return True
    return None


def git(*args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


@dataclass
class TempRepo:
    root: Path  # main checkout
    origin: Path  # bare origin
    home: Path  # sandbox for config files

    def git(self, *args, cwd=None, check=True):
        return git(*args, cwd=cwd or self.root, check=check)

    def write(self, rel, text, cwd=None):
        p = Path(cwd or self.root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
        return p

    def commit(self, msg, cwd=None):
        self.git("add", "-A", cwd=cwd)
        self.git("commit", "-q", "-m", msg, cwd=cwd)
        return self.git("rev-parse", "HEAD", cwd=cwd).stdout.strip()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolate git, ccdash user config, Claude trust and the fake claude from the real machine."""
    home = tmp_path / "home"
    home.mkdir()
    gitconfig = home / "gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = ccdash-test\n\temail = test@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n[core]\n\tautocrlf = false\n\tlongpaths = true\n"
        "[advice]\n\tdetachedHead = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("CCDASH_CONFIG", str(home / "ccdash-config.toml"))
    monkeypatch.setenv("CCDASH_CLAUDE_JSON", str(home / "claude.json"))
    monkeypatch.setenv("CCDASH_CLAUDE_SETTINGS", str(home / "claude-settings.json"))
    monkeypatch.setenv("CCDASH_CLAUDE", json.dumps([sys.executable, str(FAKE_CLAUDE)]))
    monkeypatch.setenv("CCDASH_FAKE_HOME", str(home / "fake-claude"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "claude-config"))
    monkeypatch.setenv("CCDASH_REDUCED_MOTION", "1")
    for var in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
                "CCDASH_AGENT", "CCDASH_STATE", "TMUX"):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture
def repo(sandbox, tmp_path):
    origin = tmp_path / "origin.git"
    root = tmp_path / "work" / "my-app"
    root.parent.mkdir(parents=True)
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    git("clone", "-q", str(origin), str(root), cwd=tmp_path)
    r = TempRepo(root=root, origin=origin, home=sandbox)
    r.write("README.md", "# my-app\n")
    r.write("src/ui/Toolbar.tsx", "export const Toolbar = () => null;\n")
    r.write("src/api/orders.ts", "export function orders() {\n  return [];\n}\n")
    r.write("src/auth/session.ts", "export const session = 1;\n")
    r.write(".gitignore", ".env\nnode_modules/\n")
    r.write(".worktreeinclude", ".env\n")
    r.commit("init")
    r.git("push", "-q", "-u", "origin", "main")
    r.write(".env", "SECRET=1\n")  # gitignored and listed in .worktreeinclude
    trust = {"projects": {str(root).replace("\\", "/"): {"hasTrustDialogAccepted": True}}}
    (sandbox / "claude.json").write_text(json.dumps(trust), encoding="utf-8")
    yield r
    _kill_hosts(root / ".git" / "ccdash")


def _kill_hosts(state_dir: Path):
    """Terminate any agent host left by a test, so nothing outlives the test run."""
    run = state_dir / "run"
    if not run.is_dir():
        return
    for info in run.glob("*.host.json"):
        try:
            data = json.loads(info.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for key in ("claude_pid", "host_pid"):
            pid = data.get(key)
            if not pid:
                continue
            if WINDOWS:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            else:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
    if not WINDOWS:
        subprocess.run(["tmux", "kill-server"], capture_output=True)


def wait_for(predicate, timeout=10.0, interval=0.05, message="condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {message} (last={last!r})")


@pytest.fixture
def set_scenario(sandbox, monkeypatch):
    """Give the fake claude a scenario: a list of steps, or {"agent": [...], "*": [...]}.
    Set it before starting agents; the environment is captured at spawn time."""

    def _set(steps):
        p = sandbox / "scenario.json"
        p.write_text(json.dumps(steps), encoding="utf-8")
        monkeypatch.setenv("CCDASH_FAKE_SCENARIO", str(p))
        return p

    return _set


@pytest.fixture
def ws(repo):
    """A Workspace opened on the temp repo."""
    from ccdash.workspace import Workspace

    return Workspace.open(repo.root)
