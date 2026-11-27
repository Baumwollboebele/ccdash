"""Runner backend for Claude Code's own background sessions (`runner = "bg"`, off by default).

Agent view and `claude --bg` are a research preview, so every assumption about them lives
in this file (docs: code.claude.com/docs/en/agent-view, checked against Claude Code 2.1.332):

  start     `claude --bg [flags] "<prompt>"` in the agent's worktree. It prints
            `backgrounded · <short id> · <name>`; Claude's supervisor owns the process.
  list      `claude agents --json --all --cwd <repo>`: an array with `id`, `sessionId`,
            `state` (working | blocked | done | failed | stopped) and, while the process is
            alive, `pid` and `status`. The files under ~/.claude/jobs are not an interface.
  peek      `claude logs <id>`
  stop      `claude stop <id>` (the conversation is kept)
  restart   `claude --bg --resume <session id>`
  attach    `claude attach <id>`; ← on an empty prompt or Ctrl+Z detaches

What this backend cannot do, and why:
  * Type into a session. Claude documents no scriptable input for a background session,
    so talk, broadcast and ccdash's own notices do not reach these agents: attach instead.
  * Choose the session id: `--session-id` is not documented together with `--bg`, so it is
    dropped and the id is read back from the list.
  * Rely on the environment: only PATH is documented to reach the session, so its hooks
    carry the agent's name and state dir as arguments (hooks_settings.render_command).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from .model import AgentRecord
from .repo import Repo
from .state import State, atomic_write_json

_STARTED = re.compile(r"backgrounded\s*[·.\-]\s*([0-9a-f]{6,})|claude attach ([0-9a-f]{6,})")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
LIST_TTL = 1.5  # seconds one `claude agents --json` answer is reused: every refresh asks for every agent
CLEAN_END = ("done", "stopped")


def _without(args: list[str], flag: str) -> list[str]:
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a == flag:
            skip = True
        else:
            out.append(a)
    return out


class BgRunner:
    kind = "bg"

    def __init__(self, state: State, repo: Repo, cfg=None):
        self.state = state
        self.repo = repo
        self.cfg = cfg
        self._listing: tuple[float, list[dict]] | None = None
        self._lock = threading.Lock()

    # -- the claude CLI -----------------------------------------------------------------
    def _claude(self) -> list[str]:
        from .runner import claude_command

        return claude_command(self.cfg)

    def _run(self, args: list[str], cwd: Path | str | None = None, env: dict | None = None,
             timeout: float = 30) -> subprocess.CompletedProcess:
        return subprocess.run([*self._claude(), *args], cwd=str(cwd or self.repo.main_root), env=env,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                              stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)

    def sessions(self, fresh: bool = False) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            if not fresh and self._listing is not None and now - self._listing[0] < LIST_TTL:
                return self._listing[1]
        try:
            p = self._run(["agents", "--json", "--all", "--cwd", str(self.repo.main_root)], timeout=20)
            items = json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else []
        except (OSError, subprocess.TimeoutExpired, ValueError):
            items = []
        items = [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
        with self._lock:
            self._listing = (now, items)
        return items

    def _file(self, name: str) -> Path:
        return self.state.run_dir / f"{name}.bg.json"

    def _info(self, rec: AgentRecord) -> dict | None:
        try:
            return json.loads(self._file(rec.name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _entry(self, rec: AgentRecord, fresh: bool = False) -> dict | None:
        info = self._info(rec)
        if not info:
            return None
        for it in self.sessions(fresh):
            if it.get("id") == info.get("id") or (info.get("session_id") and it.get("sessionId") == info["session_id"]):
                return it
        return None

    # -- Runner interface ---------------------------------------------------------------
    def start(self, rec, cmd, args, env, cwd, settings=None):
        from .runner import _write_settings

        self.state.ensure()
        if self.is_alive(rec):
            raise RuntimeError(f"agent {rec.name} is already running")
        args = _without(list(args), "--session-id")
        if isinstance(settings, dict):
            args = ["--settings", str(_write_settings(self.state, rec.name, settings)), *args]
        try:
            p = subprocess.run([*cmd, "--bg", *args], cwd=str(cwd), env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=90, stdin=subprocess.DEVNULL,
                               creationflags=_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"claude --bg did not start {rec.name}: {exc}") from exc
        out = f"{p.stdout or ''}\n{p.stderr or ''}"
        m = _STARTED.search(out)
        if p.returncode != 0 or not m:
            raise RuntimeError(f"claude --bg did not start {rec.name}: {out.strip()[-400:] or f'exit {p.returncode}'}")
        short = m.group(1) or m.group(2)
        session_id = None
        deadline = time.monotonic() + 5
        while session_id is None and time.monotonic() < deadline:  # the list knows the full session id
            session_id = next((it.get("sessionId") for it in self.sessions(fresh=True) if it.get("id") == short), None)
            if session_id is None:
                time.sleep(0.2)
        atomic_write_json(self._file(rec.name), {"id": short, "session_id": session_id, "started": time.time()})
        return {"session": short, "session_id": session_id}

    def is_alive(self, rec) -> bool:
        entry = self._entry(rec)
        return entry is not None and bool(entry.get("pid"))  # pid is listed only while the process lives

    def exit_info(self, rec) -> dict | None:
        entry = self._entry(rec)
        if entry is None or entry.get("pid"):
            return None
        state = entry.get("state") or ""
        return {"exitstatus": 0 if state in CLEAN_END else 1, "state": state, "killed": False}

    def peek(self, rec, lines=30):
        info = self._info(rec)
        if not info:
            return []
        try:
            p = self._run(["logs", info["id"]], timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return []
        out = [_ANSI.sub("", x).rstrip() for x in p.stdout.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        while out and not out[-1].strip():
            out.pop()
        return out[-lines:]

    def send_text(self, rec, text, enter=True) -> bool:
        return False  # no documented way to type into a background session

    def stop(self, rec, timeout=10) -> bool:
        info = self._info(rec)
        if not info or not self.is_alive(rec):
            return True
        try:
            self._run(["stop", info["id"]], timeout=timeout + 20)
        except (OSError, subprocess.TimeoutExpired):
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            entry = self._entry(rec, fresh=True)
            if entry is None or not entry.get("pid"):
                return True
            time.sleep(0.2)
        return False

    def kill(self, rec) -> None:
        self.stop(rec, timeout=5)  # `claude stop` is the only handle there is

    def attach(self, rec) -> int:
        info = self._info(rec)
        if not info:
            raise RuntimeError(f"agent {rec.name} has no background session")
        return subprocess.call([*self._claude(), "attach", info["id"]], cwd=str(self.repo.main_root))
