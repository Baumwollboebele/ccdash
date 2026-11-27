"""The agent record kept in agents.json."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, fields


def now_iso() -> str:
    t = time.time()
    lt = time.localtime(t)
    off = -(time.altzone if lt.tm_isdst > 0 else time.timezone)
    sign = "+" if off >= 0 else "-"
    off = abs(off)
    return time.strftime("%Y-%m-%dT%H:%M:%S", lt) + f".{int(t * 1000) % 1000:03d}{sign}{off // 3600:02d}:{off % 3600 // 60:02d}"


@dataclass
class AgentRecord:
    name: str
    task: str = ""
    created_at: str = field(default_factory=now_iso)
    worktree: str = ""  # absolute path
    branch: str = ""
    base_ref: str = ""
    base_sha: str = ""
    session: str = ""  # runner session id: tmux session name or host pipe name
    runner: str = ""  # winpty | tmux | bg
    claude_session_id: str | None = None
    messaging_socket: str | None = None
    model: str | None = None
    permission_mode: str | None = None
    lifecycle: str = "running"  # running | stopped | deleted
    host_pid: int | None = None
    claude_pid: int | None = None
    stop_requested: bool = False
    restarts: int = 0
    started_at: float | None = None  # epoch of the current run's start
    setup: str = ""  # "" | running | ok | failed: <code>
    adopted: bool = False
    role: str = ""  # "" for an agent in its own worktree; "lead" for the coordinator in the main checkout
    extra: dict = field(default_factory=dict, repr=False)  # unknown keys from newer versions, kept as-is

    @classmethod
    def from_dict(cls, data: dict) -> "AgentRecord":
        known = {f.name for f in fields(cls)} - {"extra"}
        rec = cls(**{k: v for k, v in data.items() if k in known})
        rec.extra = {k: v for k, v in data.items() if k not in known}
        return rec

    def to_dict(self) -> dict:
        d = asdict(self)
        extra = d.pop("extra") or {}
        return {**extra, **d}
