"""Messages: what agents send each other (SendMessage), what you send (t / b), and
ccdash's own notices; plus delivery into an agent.

Claude-specific (docs/findings.md): delivery types one line into the agent's terminal.
The messaging socket's protocol is undocumented. Claude queues text typed while it is
busy, so typing is fine at any time except while a dialog is open (needs input): then
the message waits in outbox.json until the dialog is answered.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .state import FileLock, atomic_write_json

USER_PREFIX = "[from the user]"
CCDASH_PREFIX = "[ccdash]"


@dataclass
class Message:
    t: float
    sender: str
    to: str
    text: str
    summary: str = ""
    kind: str = "agent"  # agent | you | ccdash
    delivered: bool = False
    queued: bool = False
    recipients: list[str] = field(default_factory=list)

    @property
    def first_line(self) -> str:
        for line in self.text.splitlines():
            if line.strip() and not line.strip().lower().startswith("files:"):
                return line.strip()
        return (self.text.splitlines() or [""])[0].strip()

    def involves(self, name: str | None) -> bool:
        return bool(name) and (self.sender == name or self.to == name or self.to == "all" or name in self.recipients)

    def render(self, highlight: str | None = None, width: int = 0):
        from rich.text import Text

        from .tui import theme
        from .tui.theme import ACCENT, DIM

        t = Text()
        t.append(theme.fmt_clock(self.t) + " ", style=DIM)
        def name_style(name: str, base: str) -> str:
            return f"bold {ACCENT}" if highlight and name == highlight else base

        t.append(self.sender, style=ACCENT if self.kind == "you" else (
            DIM if self.kind == "ccdash" else name_style(self.sender, "bold")))
        t.append(" → ", style=DIM)
        t.append(self.to, style=DIM if self.to == "all" else name_style(self.to, "bold"))
        if self.queued:
            t.append("  queued", style="yellow")
        elif self.kind == "agent" and self.delivered:
            t.append("  ✓", style=DIM)
        line = self.first_line
        if width and len(line) > width:
            line = line[: width - 1] + "…"
        t.append("\n  " + line, style="italic " + DIM if self.kind == "ccdash" else "default")
        return t


class MessageLog:
    def __init__(self, limit: int = 2000):
        self.items: list[Message] = []
        self.limit = limit

    def apply(self, line: dict) -> bool:
        event = line.get("event")
        t = float(line.get("t") or time.time())
        if event == "PreToolUse" and line.get("tool") == "SendMessage":
            msg = line.get("msg") or {}
            self._add(Message(t=t, sender=line.get("agent", "?"), to=msg.get("to") or "?", text=msg.get("text", ""),
                              summary=msg.get("summary", "")))
            return True
        if event == "UserPromptSubmit" and line.get("incoming"):
            inc = line["incoming"]
            receiver, sender, text = line.get("agent"), inc.get("from"), inc.get("text", "")
            for m in reversed(self.items[-200:]):
                if m.kind != "agent" or m.sender != sender or m.text.strip() != text.strip():
                    continue
                if m.to in ("all", "*"):  # a broadcast arrives once per receiver: that is how we learn who got it
                    if receiver and receiver not in m.recipients:
                        m.recipients.append(receiver)
                    m.delivered = True
                    return True
                if not m.delivered and (m.to == receiver or receiver in m.recipients):
                    m.delivered = True
                    return True
            # sent by a session ccdash does not track (or the send event was lost): still show it
            self._add(Message(t=t, sender=sender or "?", to=receiver or "?", text=text, delivered=True))
            return True
        if event == "ccdash.message":
            sender = line.get("from", "you")  # you, ccdash, or an agent that ran `ccdash send` (the lead)
            self._add(Message(t=t, sender=sender, to=line.get("to", "?"), text=line.get("text", ""),
                              kind=sender if sender in ("you", "ccdash") else "agent", delivered=not line.get("queued"),
                              queued=bool(line.get("queued")), recipients=line.get("recipients", [])))
            return True
        if event == "ccdash.delivered":
            for m in reversed(self.items[-200:]):
                if m.queued and m.to == line.get("to") and m.text == line.get("text"):
                    m.queued, m.delivered = False, True
                    return True
        return False

    def _add(self, m: Message) -> None:
        self.items.append(m)
        if len(self.items) > self.limit:
            del self.items[: len(self.items) - self.limit]


# -- delivery ---------------------------------------------------------------------------
def one_line(text: str) -> str:
    return " ".join(text.split())


def _dashboard(ws, dashboard):
    if dashboard is not None:
        return dashboard
    from .dashboard import Dashboard

    d = Dashboard(ws)
    d.refresh_runtime()
    return d


def _outbox_path(ws):
    return ws.state.root / "outbox.json"


def _outbox_edit(ws, fn):
    with FileLock(ws.state.root / "outbox.lock", timeout=5):
        try:
            items = json.loads(_outbox_path(ws).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            items = []
        items = fn(items)
        atomic_write_json(_outbox_path(ws), items)
        return items


def _deliver(ws, name: str, typed: str) -> bool:
    rec = ws.record(name)
    return ws.runner.send_text(rec, typed)


def _send(ws, name: str, text: str, sender: str, d) -> str:
    from .workspace import WorkspaceError

    v = d.get(name)
    if v is None or v.status in ("untracked", "deleted"):
        raise WorkspaceError(f"no agent named {name!r}")
    if not v.alive:
        raise WorkspaceError(f"{name} is not running; restart it first (r)")
    typed = f"{prefix_for(sender)} {text}"
    if v.status == "needs_input":
        _outbox_edit(ws, lambda items: items + [{"to": name, "typed": typed, "text": text, "from": sender,
                                                 "t": time.time()}])
        return "queued"
    return "sent" if _deliver(ws, name, typed) else "failed"


def _cannot_type(name: str):
    from .workspace import WorkspaceError

    return WorkspaceError(f"cannot type into {name}: its session takes no input from outside (a background "
                          f"session, or it just exited). Attach to it instead.")


def prefix_for(sender: str) -> str:
    """What a typed line starts with, so the agent knows who is talking: you, ccdash, or another
    agent whose session ran `ccdash send` (the lead does)."""
    if sender == "you":
        return USER_PREFIX
    return CCDASH_PREFIX if sender == "ccdash" else f"[from {sender}]"


def send_from_user(ws, name: str, text: str, dashboard=None, sender: str = "you") -> str:
    text = one_line(text)
    if not text:
        return "nothing to send"
    d = _dashboard(ws, dashboard)
    res = _send(ws, name, text, sender, d)
    if res == "failed":
        raise _cannot_type(name)
    ws.state.append_event({"agent": name, "event": "ccdash.message", "from": sender, "to": name, "text": text,
                           "queued": res == "queued", "t": time.time()})
    return (f"{name} is waiting for a permission answer; message queued" if res == "queued" else f"sent to {name}")


def _running(d) -> list[str]:
    return [v.name for v in d.sorted_views() if v.alive and v.status not in ("untracked", "deleted")]


def broadcast_from_user(ws, text: str, dashboard=None, sender: str = "you", exclude: set | None = None) -> list[str]:
    text = one_line(text)
    d = _dashboard(ws, dashboard)
    out, recipients, queued = [], [], False
    for name in _running(d):
        if exclude and name in exclude:
            continue
        res = _send(ws, name, text, sender, d)
        if res != "failed":
            recipients.append(name)
        queued |= res == "queued"
        out.append(f"{res} {'to' if res == 'sent' else 'for'} {name}")
    if recipients:
        ws.state.append_event({"agent": "*", "event": "ccdash.message", "from": sender, "to": "all", "text": text,
                               "recipients": recipients, "queued": False, "t": time.time()})
    return out


def notice(ws, to: str, text: str, dashboard=None) -> str:
    """A ccdash notice to one agent (or 'all')."""
    text = one_line(text)
    if to == "all":
        broadcast_from_user(ws, text, dashboard=dashboard, sender="ccdash")
        return "sent"
    d = _dashboard(ws, dashboard)
    res = _send(ws, to, text, "ccdash", d)
    if res == "failed":
        raise _cannot_type(to)
    ws.state.append_event({"agent": to, "event": "ccdash.message", "from": "ccdash", "to": to, "text": text,
                           "queued": res == "queued", "t": time.time()})
    return res


def flush_outbox(ws, dashboard) -> int:
    """Deliver queued messages to agents whose dialog has been answered."""
    path = _outbox_path(ws)
    if not path.exists():
        return 0
    sent = []

    def take(items):
        keep = []
        for it in items:
            v = dashboard.get(it["to"])
            if v is None or not v.alive:
                if time.time() - it.get("t", 0) > 3600:
                    continue  # agent gone for an hour: drop
                keep.append(it)
            elif v.status == "needs_input":
                keep.append(it)
            else:
                sent.append(it)
        return keep

    _outbox_edit(ws, take)
    for it in sent:
        try:
            _deliver(ws, it["to"], it["typed"])
            ws.state.append_event({"agent": it["to"], "event": "ccdash.delivered", "to": it["to"], "text": it["text"],
                                   "t": time.time()})
        except Exception:  # noqa: BLE001
            pass
    return len(sent)
