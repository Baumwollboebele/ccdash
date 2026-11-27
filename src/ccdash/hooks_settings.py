"""The settings each agent gets with --settings: its hook registration.

Two transports (docs/findings.md):
  * http    (Windows host): every event but SessionStart posts to the agent's own host on
            127.0.0.1 - no process per event, synchronous and in order.
  * command (tmux, or when the user restricts HTTP hooks): exec-form command hooks running
            ccdash.hook on the base interpreter with -I -S; async except SessionEnd.
SessionStart supports only command hooks, so it is a command hook in both.
"""

from __future__ import annotations

from pathlib import Path

HTTP_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest",
               "PermissionDenied", "Notification", "Stop", "StopFailure", "SessionEnd")
COMMAND_EVENTS = ("SessionStart", *HTTP_EVENTS)
SYNC_EVENTS = frozenset({"SessionEnd"})  # synchronous so the event is not lost when claude exits


def _package_parent() -> str:
    import ccdash

    return str(Path(ccdash.__file__).resolve().parent.parent)


def command_hook(event: str, sync: bool | None = None, ident: tuple[str, str, str] | None = None) -> dict:
    """`ident` = (agent, state dir, worktree) as arguments, for sessions that do not get our environment."""
    from .runner import python_exe

    boot = f"import sys;sys.path.insert(0,{_package_parent()!r});from ccdash.hook import main;main()"
    is_sync = event in SYNC_EVENTS if sync is None else sync
    return {"type": "command", "command": python_exe(), "args": ["-I", "-S", "-c", boot, event, *(ident or ())],
            "timeout": 10, "async": not is_sync}


def render_command(guard: bool = False, ident: tuple[str, str, str] | None = None) -> dict:
    """Guard mode needs PreToolUse to be synchronous: an async hook cannot return a decision."""
    hooks = {}
    for ev in COMMAND_EVENTS:
        h = command_hook(ev, sync=True if (guard and ev == "PreToolUse") else None, ident=ident)
        hooks[ev] = [{"hooks": [h]}]
    return {"hooks": hooks}


def render_http(port: int, token: str) -> dict:
    hooks = {"SessionStart": [{"hooks": [command_hook("SessionStart")]}]}
    for ev in HTTP_EVENTS:
        hooks[ev] = [{"hooks": [{"type": "http", "url": f"http://127.0.0.1:{port}/hook/{token}/{ev}",
                                 "timeout": 3 if ev == "SessionEnd" else 5}]}]
    return {"hooks": hooks}


def transport(ws) -> str:
    from .runner import allowed_http_hook_urls_set

    want = ws.cfg.hook_transport
    if allowed_http_hook_urls_set():
        return "command"
    if want == "auto":
        want = "http" if ws.runner.kind == "winpty" else "command"
    if want == "http" and ws.runner.kind != "winpty":
        return "command"  # only the Windows host serves HTTP hooks
    return want


def for_agent(ws, rec):
    """'http' (the host renders the settings once it knows its port) or a settings dict."""
    if transport(ws) == "http":
        return "http"
    ident = (rec.name, str(ws.state.root), str(rec.worktree or "")) if ws.runner.kind == "bg" else None
    return render_command(guard=ws.cfg.guard != "off", ident=ident)
