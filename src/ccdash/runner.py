"""Runner backends: how an agent's interactive `claude` is kept alive, typed into,
peeked at and attached to.

Claude-specific assumptions live here (with hook.py and messaging.py):
the environment a parent Claude session leaks, workspace trust in ~/.claude.json,
transcript locations, and the keystrokes that stop a session.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Protocol

from .model import AgentRecord
from .repo import Repo
from .state import State, atomic_write_json

WINDOWS = sys.platform == "win32"

# Exported by a running Claude Code session to its children. An agent started from a
# shell inside Claude Code must not inherit its parent's identity or messaging inbox.
SCRUB_VARS = (
    "CLAUDECODE",
    "CLAUDE_PID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_ENV_FILE",
    "CLAUDE_PROJECT_DIR",
    "CCDASH_AGENT",
    "CCDASH_STATE",
)

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


# -- claude command line and environment --------------------------------------------
def claude_command(cfg=None) -> list[str]:
    env = os.environ.get("CCDASH_CLAUDE")
    if env:
        return json.loads(env) if env.lstrip().startswith("[") else [env]
    if cfg is not None and cfg.claude_command:
        return list(cfg.claude_command)
    return ["claude"]


def claude_args(name: str, *, session_id: str | None = None, resume: str | None = None, model: str | None = None,
                permission_mode: str | None = None, prompt: str | None = None) -> list[str]:
    args = ["--name", name]
    if resume:
        args += ["--resume", resume]
    elif session_id:
        args += ["--session-id", session_id]
    if model:
        args += ["--model", model]
    if permission_mode:
        args += ["--permission-mode", permission_mode]
    if prompt:
        args.append(prompt)
    return args


def agent_env(state: State, name: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_VARS}
    if os.environ.get("CLAUDECODE"):
        # Claude Code exports NO_COLOR to its tool shells; an agent started from there
        # should still look like a normal session. A user's own NO_COLOR is kept.
        env.pop("NO_COLOR", None)
    env["CCDASH_AGENT"] = name
    env["CCDASH_STATE"] = str(state.root)
    env.setdefault("COLORTERM", "truecolor")
    return env


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _claude_json_path() -> Path:
    if os.environ.get("CCDASH_CLAUDE_JSON"):
        return Path(os.environ["CCDASH_CLAUDE_JSON"])
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
    return Path.home() / ".claude.json"


def _norm(p: str) -> str:
    p = p.replace("\\", "/").rstrip("/")
    return p.lower() if WINDOWS else p


def is_trusted(main_root: Path) -> bool | None:
    """Has the workspace trust dialog been accepted for this repo (or a parent)?
    None when Claude's state file cannot be read, so callers do not block on it."""
    try:
        data = json.loads(_claude_json_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    projects = {_norm(k): v for k, v in (data.get("projects") or {}).items() if isinstance(v, dict)}
    path = _norm(str(main_root))
    while path:
        if projects.get(path, {}).get("hasTrustDialogAccepted"):
            return True
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent == path:
            break
        path = parent
    return False


def transcript_path(cwd: Path, session_id: str) -> Path:
    mangled = re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))
    return claude_config_dir() / "projects" / mangled / f"{session_id}.jsonl"


def session_exists(cwd: Path, session_id: str | None) -> bool:
    return bool(session_id) and transcript_path(cwd, session_id).exists()


def allowed_http_hook_urls_set() -> bool:
    """True if the user's Claude settings restrict HTTP hooks (then ccdash uses command hooks)."""
    p = Path(os.environ.get("CCDASH_CLAUDE_SETTINGS") or claude_config_dir() / "settings.json")
    try:
        return "allowedHttpHookUrls" in json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False


# -- interpreter bootstrap ----------------------------------------------------------
def python_exe() -> str:
    """The real interpreter: a venv's python.exe on Windows is a launcher that adds a process."""
    return getattr(sys, "_base_executable", None) or sys.executable


def pyboot(module: str, func: str, args: list[str]) -> list[str]:
    """argv that runs module.func() in a fresh isolated interpreter with our sys.path."""
    paths = [p for p in sys.path if p and os.path.isabs(p)]
    boot = f"import sys;sys.path[:0]={paths!r};from {module} import {func} as _m;_m()"
    return [python_exe(), "-I", "-S", "-c", boot, *args]


def spawn_detached(argv: list[str], log_path: Path | None = None, env: dict | None = None) -> subprocess.Popen:
    log = open(log_path, "ab") if log_path else subprocess.DEVNULL
    kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, env=env)
    try:
        if WINDOWS:
            flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            try:
                return subprocess.Popen(argv, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
            except OSError:  # the job (e.g. a terminal host) forbids breakaway
                return subprocess.Popen(argv, creationflags=flags, **kwargs)
        return subprocess.Popen(argv, start_new_session=True, **kwargs)
    finally:
        if log_path:
            log.close()


# -- process helpers ----------------------------------------------------------------
if WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _k32.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    class _PE32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    def process_created(pid: int) -> int | None:
        h = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            t = [wintypes.FILETIME() for _ in range(4)]
            if not _k32.GetProcessTimes(h, *(ctypes.byref(x) for x in t)):
                return None
            return (t[0].dwHighDateTime << 32) | t[0].dwLowDateTime
        finally:
            _k32.CloseHandle(h)

    def pid_alive(pid: int | None, created: int | None = None) -> bool:
        if not pid:
            return False
        h = _k32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not _k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
                return False
        finally:
            _k32.CloseHandle(h)
        return created is None or process_created(pid) == created

    def parent_pid(pid: int) -> int | None:
        snap = _k32.CreateToolhelp32Snapshot(0x2, 0)
        try:
            e = _PE32()
            e.dwSize = ctypes.sizeof(_PE32)
            ok = _k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                if e.th32ProcessID == pid:
                    return e.th32ParentProcessID
                ok = _k32.Process32NextW(snap, ctypes.byref(e))
        finally:
            _k32.CloseHandle(snap)
        return None

    def kill_tree(pid: int | None) -> None:
        if pid:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
else:
    def process_created(pid: int) -> int | None:
        try:
            with open(f"/proc/{pid}/stat") as f:
                return int(f.read().rsplit(")", 1)[1].split()[19])
        except (OSError, ValueError, IndexError):
            return None

    def pid_alive(pid: int | None, created: int | None = None) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return created is None or process_created(pid) in (None, created)

    def parent_pid(pid: int) -> int | None:
        try:
            with open(f"/proc/{pid}/stat") as f:
                return int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None

    def kill_tree(pid: int | None) -> None:
        if pid:
            try:
                os.killpg(os.getpgid(pid), 9)
            except OSError:
                pass


# -- runner interface ---------------------------------------------------------------
class Runner(Protocol):
    kind: str
    state: State

    def start(self, rec: AgentRecord, cmd: list[str], args: list[str], env: dict, cwd: Path,
              settings: dict | str | None = None) -> dict: ...

    def stop(self, rec: AgentRecord, timeout: float = 10) -> bool: ...

    def kill(self, rec: AgentRecord) -> None: ...

    def is_alive(self, rec: AgentRecord) -> bool: ...

    def exit_info(self, rec: AgentRecord) -> dict | None: ...

    def peek(self, rec: AgentRecord, lines: int = 30) -> list[str]: ...

    def send_text(self, rec: AgentRecord, text: str, enter: bool = True) -> bool: ...

    def attach(self, rec: AgentRecord) -> int: ...


def _remove(path: Path, attempts: int = 50) -> None:
    """Delete a run file; on Windows a reader may hold it open for a moment."""
    for _ in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.02)
    path.unlink(missing_ok=True)


def _write_settings(state: State, name: str, settings: dict) -> Path:
    path = state.run_dir / f"{name}.settings.json"
    atomic_write_json(path, settings)
    return path


class WinPtyRunner:
    """Each agent runs under a detached `ccdash host` that owns a ConPTY (see host.py)."""

    kind = "winpty"

    def __init__(self, state: State, repo: Repo):
        self.state = state
        self.repo = repo

    def pipe_name(self, name: str) -> str:
        return rf"\\.\pipe\ccdash-{self.repo.key}-{name}"

    def _files(self, name: str) -> dict[str, Path]:
        r = self.state.run_dir
        return {k: r / f"{name}.{k}.json" for k in ("spec", "host", "exit", "error")}

    def host_info(self, rec: AgentRecord) -> dict | None:
        try:
            return json.loads(self._files(rec.name)["host"].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def start(self, rec, cmd, args, env, cwd, settings=None):
        self.state.ensure()
        files = self._files(rec.name)
        if self.is_alive(rec):
            raise RuntimeError(f"agent {rec.name} is already running")
        for k in ("host", "exit", "error"):
            _remove(files[k])
        if isinstance(settings, dict):
            args = ["--settings", str(_write_settings(self.state, rec.name, settings)), *args]
        # The host inherits this process's environment; the spec carries only the
        # differences, so secrets in the environment are never written to disk.
        spec = {
            "name": rec.name,
            "cwd": str(cwd),
            "cmd": list(cmd),
            "args": list(args),
            "env_set": {k: v for k, v in env.items() if os.environ.get(k) != v},
            "env_unset": [k for k in os.environ if k not in env],
            "pipe": self.pipe_name(rec.name),
            "authkey": secrets.token_hex(32),
            "settings": settings if settings == "http" else None,
            "state_dir": str(self.state.root),
            "worktree": rec.worktree or str(cwd),
            "rows": 35,
            "cols": 120,
        }
        atomic_write_json(files["spec"], spec)
        spawn_detached(pyboot("ccdash.host", "launch", [str(files["spec"])]),
                       log_path=self.state.logs_dir / f"{rec.name}.host.log")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            info = self.host_info(rec)
            if info and info.get("claude_pid"):
                return info
            if files["error"].exists():
                raise RuntimeError(files["error"].read_text(encoding="utf-8").strip())
            time.sleep(0.03)
        raise RuntimeError(f"host for {rec.name} did not start; see {self.state.logs_dir / (rec.name + '.host.log')}")

    def is_alive(self, rec) -> bool:
        files = self._files(rec.name)
        info = self.host_info(rec)
        if not info or files["exit"].exists():
            return False
        return pid_alive(info.get("host_pid"), info.get("host_created"))

    def exit_info(self, rec) -> dict | None:
        try:
            return json.loads(self._files(rec.name)["exit"].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def call(self, rec, req: dict, timeout: float = 5.0) -> dict | None:
        from . import pipeproto

        info = self.host_info(rec)
        if not info or not self.is_alive(rec):
            return None
        try:
            return pipeproto.request(info, req, timeout=timeout)
        except (OSError, EOFError, TimeoutError):
            return None

    def peek(self, rec, lines=30):
        resp = self.call(rec, {"op": "screen", "history": lines})
        if not resp:
            return []
        out = resp.get("history", []) + resp.get("lines", [])
        while out and not out[-1].strip():
            out.pop()
        return out[-lines:]

    def send_text(self, rec, text, enter=True) -> bool:
        return bool(self.call(rec, {"op": "type", "text": text, "enter": enter}))

    def write_raw(self, rec, data: str) -> bool:
        return bool(self.call(rec, {"op": "write", "data": data}))

    def stop(self, rec, timeout=10) -> bool:
        if not self.is_alive(rec):
            return True
        self.call(rec, {"op": "stop", "timeout": timeout})
        deadline = time.monotonic() + timeout + 3
        while time.monotonic() < deadline:
            if not self.is_alive(rec):
                return True
            time.sleep(0.05)
        self.kill(rec)
        return not self.is_alive(rec)

    def kill(self, rec) -> None:
        info = self.host_info(rec) or {}
        if not self.call(rec, {"op": "kill"}, timeout=2):
            kill_tree(info.get("claude_pid"))
            kill_tree(info.get("host_pid"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.is_alive(rec):
            time.sleep(0.05)
        if self.is_alive(rec):
            kill_tree(info.get("claude_pid"))
            kill_tree(info.get("host_pid"))

    def attach(self, rec) -> int:
        from .attach import run_console

        info = self.host_info(rec)
        if not info or not self.is_alive(rec):
            raise RuntimeError(f"agent {rec.name} is not running")
        return run_console(info, title=rec.name)


class TmuxRunner:
    """Each agent runs in its own tmux session (macOS, Linux, WSL)."""

    kind = "tmux"

    def __init__(self, state: State, repo: Repo):
        self.state = state
        self.repo = repo

    def session_name(self, name: str) -> str:
        return f"ccdash-{self.repo.key}-{name}"

    def _exit_file(self, name: str) -> Path:
        return self.state.run_dir / f"{name}.exit.json"

    def _tmux(self, *args, check=False) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check)

    def start(self, rec, cmd, args, env, cwd, settings=None):
        self.state.ensure()
        if self.is_alive(rec):
            raise RuntimeError(f"agent {rec.name} is already running")
        exit_file = self._exit_file(rec.name)
        _remove(exit_file)
        if isinstance(settings, dict):
            args = ["--settings", str(_write_settings(self.state, rec.name, settings)), *args]
        sess = self.session_name(rec.name)
        wrapper = ('"$@"; code=$?; printf \'{"exitstatus": %d, "ended": %s}\' "$code" "$(date +%s)" '
                   '> "$CCDASH_EXIT_FILE"; exit $code')
        e = []
        for k in ("CCDASH_AGENT", "CCDASH_STATE", "COLORTERM", "PATH", "HOME"):
            if k in env:
                e += ["-e", f"{k}={env[k]}"]
        e += ["-e", f"CCDASH_EXIT_FILE={exit_file}"]
        for var in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET"):
            self._tmux("set-environment", "-g", "-u", var)
        self._tmux("new-session", "-d", "-s", sess, "-x", "120", "-y", "35", "-c", str(cwd), *e,
                   "sh", "-c", wrapper, "sh", *cmd, *args, check=True)
        return {"session": sess}

    def is_alive(self, rec) -> bool:
        return self._tmux("has-session", "-t", f"={self.session_name(rec.name)}").returncode == 0 and \
            not self._exit_file(rec.name).exists()

    def exit_info(self, rec):
        try:
            return json.loads(self._exit_file(rec.name).read_text())
        except (OSError, ValueError):
            return None

    def peek(self, rec, lines=30):
        p = self._tmux("capture-pane", "-p", "-t", f"={self.session_name(rec.name)}", "-S", f"-{lines}")
        out = p.stdout.splitlines() if p.returncode == 0 else []
        while out and not out[-1].strip():
            out.pop()
        return out[-lines:]

    def send_text(self, rec, text, enter=True) -> bool:
        target = f"={self.session_name(rec.name)}"
        if self._tmux("send-keys", "-t", target, "-l", text).returncode != 0:
            return False
        if enter:
            time.sleep(0.15)
            self._tmux("send-keys", "-t", target, "Enter")
        return True

    def stop(self, rec, timeout=10) -> bool:
        if not self.is_alive(rec):
            return True
        target = f"={self.session_name(rec.name)}"
        self._tmux("send-keys", "-t", target, "Escape")
        time.sleep(0.3)
        self._tmux("send-keys", "-t", target, "-l", "/exit")
        time.sleep(0.3)
        self._tmux("send-keys", "-t", target, "Enter")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_alive(rec):
                self._tmux("kill-session", "-t", target)
                return True
            time.sleep(0.1)
        self.kill(rec)
        return True

    def kill(self, rec) -> None:
        self._tmux("kill-session", "-t", f"={self.session_name(rec.name)}")

    def attach(self, rec) -> int:
        sess = self.session_name(rec.name)
        if os.environ.get("TMUX"):
            return subprocess.call(["tmux", "switch-client", "-t", f"={sess}"])
        return subprocess.call(["tmux", "attach-session", "-t", f"={sess}"])


def get_runner(cfg, state: State, repo: Repo) -> Runner:
    kind = cfg.runner
    if kind == "auto":
        kind = "winpty" if WINDOWS else "tmux"
    if kind == "winpty":
        if not WINDOWS:
            raise RuntimeError("runner = 'winpty' needs native Windows")
        return WinPtyRunner(state, repo)
    if kind == "tmux":
        if shutil.which("tmux") is None:
            raise RuntimeError("tmux is not installed (needed for the tmux runner)")
        return TmuxRunner(state, repo)
    if kind == "bg":
        from .bgrunner import BgRunner

        return BgRunner(state, repo)
    raise RuntimeError(f"unknown runner {kind!r}")
