"""The state directory: agents.json registry, events.jsonl, run/ and logs/.

agents.json is written only by ccdash (CLI/TUI) under agents.lock, via temp file + rename.
events.jsonl is append-only; every writer holds events.lock for the duration of one write().
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .model import AgentRecord, now_iso


class LockTimeout(Exception):
    pass


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class FileLock:
    """Exclusive lock on a lock file, usable across processes and threads."""

    def __init__(self, path: Path | str, timeout: float = 5.0):
        self.path = str(path)
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self) -> "FileLock":
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.timeout
        delay = 0.002
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                os.close(fd)
                raise LockTimeout(self.path)
            time.sleep(delay)
            delay = min(delay * 2, 0.05)
        self.fd = fd
        return self

    def __exit__(self, *exc) -> None:
        if self.fd is not None:
            try:
                _unlock(self.fd)
            finally:
                os.close(self.fd)
                self.fd = None


def atomic_write_json(path: Path, data) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a reader has the file open for a moment
            time.sleep(0.01 * (attempt + 1))
    os.replace(tmp, path)


class State:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    @property
    def agents_path(self) -> Path:
        return self.root / "agents.json"

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def ownership_path(self) -> Path:
        return self.root / "ownership.json"

    @property
    def hooks_path(self) -> Path:
        return self.root / "hooks.json"

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    @property
    def run_dir(self) -> Path:
        return self.root / "run"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    def ensure(self) -> None:
        for d in (self.root, self.run_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    def append_event(self, event: dict) -> None:
        """Append one event line written by ccdash itself (notices, your messages, lifecycle)."""
        event.setdefault("ts", now_iso())
        line = json.dumps(event, ensure_ascii=False) + "\n"
        self.ensure()
        with FileLock(self.root / "events.lock", timeout=2):
            with open(self.events_path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line)


class Registry:
    def __init__(self, state: State):
        self.state = state
        self._lock_path = state.root / "agents.lock"

    def _read(self) -> str | None:
        """agents.json as text; None if there is none. Readers take no lock, and on Windows the file
        cannot be opened for the instant in which a writer replaces it: try again instead of failing."""
        for attempt in range(40):
            try:
                return self.state.agents_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.005 * (attempt + 1))
        return None

    def load(self) -> dict[str, AgentRecord]:
        text = self._read()
        if text is None:
            return {}
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise RuntimeError(f"{self.state.agents_path} is not valid JSON: {exc}") from exc
        return {a["name"]: AgentRecord.from_dict(a) for a in data.get("agents", [])}

    def get(self, name: str) -> AgentRecord | None:
        return self.load().get(name)

    def _save(self, agents: dict[str, AgentRecord]) -> None:
        self.state.ensure()
        atomic_write_json(self.state.agents_path,
                          {"version": 1, "agents": [a.to_dict() for a in agents.values()]})

    @contextmanager
    def edit(self) -> Iterator[dict[str, AgentRecord]]:
        self.state.ensure()
        with FileLock(self._lock_path, timeout=10):
            agents = self.load()
            yield agents
            self._save(agents)

    def put(self, rec: AgentRecord) -> None:
        with self.edit() as agents:
            agents[rec.name] = rec

    def update(self, name: str, **changes) -> AgentRecord:
        with self.edit() as agents:
            rec = agents[name]
            for k, v in changes.items():
                setattr(rec, k, v)
            return rec


class EventReader:
    """Incremental reader of events.jsonl: returns only complete new lines."""

    def __init__(self, path: Path, offset: int = 0):
        self.path = path
        self.offset = offset

    def read_new(self) -> list[dict]:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return []
        if size < self.offset:  # truncated or replaced: start over
            self.offset = 0
        if size == self.offset:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            chunk = f.read(size - self.offset)
        end = chunk.rfind(b"\n")
        if end < 0:
            return []
        self.offset += end + 1
        events = []
        for raw in chunk[: end + 1].splitlines():
            try:
                events.append(json.loads(raw))
            except ValueError:
                continue
        return events
