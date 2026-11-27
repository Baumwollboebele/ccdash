"""The lead: one Claude Code session per repository that you talk to instead of setting up
agents by hand. It runs in the main checkout, writes no code itself, and turns what you ask
for into agents by running `ccdash new "<task>"`; each shows up in the dashboard at once.

    l in the dashboard, or `cc lead "work on feature xyz"`

The lead is an ordinary agent record with role "lead": no worktree and no branch of its
own, so nothing of it can be merged or removed from disk. It may run the harmless ccdash
commands (new, ls, peek, send, broadcast) without a permission prompt; stopping, deleting,
merging and pushing still ask you (see runner.allow_patterns).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

LEAD = "lead"
TASK = "Your coordinator · press l and say what to build"  # the table shows the first words
HARMLESS = ("new", "ls", "peek", "send", "broadcast")  # what the lead may run without asking
NO_REQUEST = "None yet. Say in one line that you are ready, then wait for the user."


def command() -> tuple[str, str | None]:
    """How the lead calls ccdash: (`ccdash`, the directory to put on its PATH), or the full
    interpreter command when ccdash runs from source without its entry point installed."""
    scripts = Path(sys.executable).parent
    for name in ("ccdash.exe", "ccdash"):
        if (scripts / name).exists():
            return "ccdash", str(scripts)
    return f'"{Path(sys.executable).as_posix()}" -m ccdash', None


def tell(ws, text: str | None = None, dashboard=None) -> str:
    """Give the lead a request: start it with the request, or type it into the running one."""
    from . import messaging

    text = messaging.one_line(text or "")
    rec = ws.lead()
    if rec is not None and ws.runner.is_alive(rec):
        if not text:
            return "The lead is running. Tell it what you want with l, or attach with a."
        res = messaging.send_from_user(ws, LEAD, text, dashboard=dashboard)
        return "Told the lead." if res.startswith("sent") else res  # or: queued behind a permission dialog
    ws.start_lead(text or None)
    if text:  # it went in with the prompt, not through messaging: log it, so Messages shows what you asked
        ws.state.append_event({"agent": LEAD, "event": "ccdash.message", "from": "you", "to": LEAD, "text": text,
                               "queued": False, "t": time.time()})
    return "Started the lead with your request." if text else \
        "Started the lead. Tell it what you want with l, or attach with a."
