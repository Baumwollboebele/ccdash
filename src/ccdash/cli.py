"""`cc`: open the dashboard, or manage agents from the shell.

    cc                      open the dashboard for the repo you are in
    cc lead "request"       tell the lead what you want built; it starts the agents
    cc "task"               start one agent for the task yourself (also: cc new "task" --name x)
    cc ls [--json] [--all]  list agents
    cc stop|restart|attach|peek|rm|adopt <name>
    cc send <name> <text>   type a one-line message into an agent;  cc broadcast <text>
    cc hook <event>         (internal) hook entry point, reads the hook JSON on stdin

Only the dashboard imports Textual; everything else stays fast.
"""

from __future__ import annotations

import argparse
import json
import sys

SUBCOMMANDS = ("ls", "new", "lead", "stop", "restart", "attach", "peek", "rm", "delete", "adopt", "send", "broadcast",
               "hook", "version", "dash", "help")


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(f"cc: {text}\n")


def _ws(args=None):
    from .workspace import Workspace

    return Workspace.open()


def _status_glyph(status: str) -> str:
    return {"working": "●", "running": "●", "needs_input": "◐", "finished": "✓", "idle": "○", "stopped": "■",
            "crashed": "✕", "setup": "…", "starting": "…", "untracked": "?"}.get(status, " ")


def cmd_new(ns) -> int:
    ws = _ws()
    rec = ws.new_agent(" ".join(ns.task), name=ns.name, base=ns.base, model=ns.model,
                       permission_mode=ns.permission_mode)
    _out(f"started {rec.name} · {rec.branch} · {ws.repo.rel(rec.worktree)}")
    _out(f"  attach with: cc attach {rec.name}")
    return 0


def cmd_lead(ns) -> int:
    from . import lead

    _out(lead.tell(_ws(), " ".join(ns.text)))
    _out("  attach with: cc attach lead")
    return 0


def cmd_ls(ns) -> int:
    from .dashboard import Dashboard

    ws = _ws()
    d = Dashboard(ws)
    d.show_hidden = ns.all
    d.refresh_all()
    views = d.sorted_views()
    if ns.json:
        _out(json.dumps([v.to_json() for v in views], indent=1, ensure_ascii=False))
        return 0
    if not views:
        _out(f"no agents in {ws.repo.name}. Start one with: cc \"<task>\"")
        return 0
    rows = [("NAME", "STATUS", "BRANCH", "↑↓ MAIN", "FILES", "STATE", "TASK")]
    for v in views:
        rows.append((v.name, f"{_status_glyph(v.status)} {v.status.replace('_', ' ')}", v.branch,
                     f"+{v.ahead} −{v.behind}", str(v.files), v.merge_state, v.task.splitlines()[0][:50] if v.task else ""))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        _out("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    return 0


def cmd_stop(ns) -> int:
    ws = _ws()
    ok = ws.stop(ns.name, wait=not ns.no_wait)
    _out(f"stopped {ns.name}" if ok else f"{ns.name} did not exit cleanly; it was killed")
    return 0


def cmd_restart(ns) -> int:
    ws = _ws()
    rec = ws.restart(ns.name)
    _out(f"restarted {rec.name}" + (" (resumed)" if rec.claude_session_id else ""))
    return 0


def cmd_attach(ns) -> int:
    return _ws().attach(ns.name)


def cmd_peek(ns) -> int:
    for line in _ws().peek(ns.name, lines=ns.lines):
        _out(line)
    return 0


def cmd_rm(ns) -> int:
    from .workspace import ConfirmationRequired

    ws = _ws()
    check = ws.delete_check(ns.name)
    confirm = ns.yes
    if check.needs_confirmation and confirm != ns.name:
        _err(f"deleting {ns.name} would lose:")
        for item in check.lost:
            sys.stderr.write(f"  - {item}\n")
        if sys.stdin.isatty() and confirm is None:
            confirm = input(f"type '{ns.name}' to delete anyway: ").strip()
        if confirm != ns.name:
            _err("not deleted")
            return 1
    try:
        ws.delete(ns.name, confirm=confirm)
    except ConfirmationRequired as exc:
        _err(str(exc))
        return 1
    _out(f"deleted {ns.name} (worktree and branch removed)")
    return 0


def cmd_adopt(ns) -> int:
    rec = _ws().adopt(ns.name)
    _out(f"adopted {rec.name} ({rec.branch}); restart it with: cc restart {rec.name}")
    return 0


def cmd_send(ns) -> int:
    from . import messaging

    ws = _ws()
    res = messaging.send_from_user(ws, ns.name, " ".join(ns.text))
    _out(res)
    return 0


def cmd_broadcast(ns) -> int:
    from . import messaging

    ws = _ws()
    for line in messaging.broadcast_from_user(ws, " ".join(ns.text)):
        _out(line)
    return 0


def cmd_hook(ns) -> int:
    from . import hook

    hook.main([ns.event])
    return 0


def cmd_dash(ns) -> int:
    from .tui.app import run

    return run(no_anim=ns.no_anim)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cc", description="Dashboard for parallel Claude Code agents.")
    p.add_argument("--no-anim", action="store_true", help="turn all animation into static state changes")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("lead", help="tell the lead what you want built; it starts the agents")
    s.add_argument("text", nargs="*", help="the request; without one, the lead is just started")
    s.set_defaults(func=cmd_lead)
    s = sub.add_parser("new", help="start an agent for a task (the same as: cc \"<task>\")")
    s.add_argument("task", nargs="+")
    s.add_argument("--name")
    s.add_argument("--base", help="branch to start from (default: the default branch; HEAD for the main checkout)")
    s.add_argument("--model")
    s.add_argument("--permission-mode")
    s.set_defaults(func=cmd_new)
    s = sub.add_parser("ls", help="list agents")
    s.add_argument("--json", action="store_true")
    s.add_argument("--all", action="store_true", help="include deleted agents")
    s.set_defaults(func=cmd_ls)
    s = sub.add_parser("stop", help="stop an agent (keeps worktree and branch)")
    s.add_argument("name")
    s.add_argument("--no-wait", action="store_true")
    s.set_defaults(func=cmd_stop)
    s = sub.add_parser("restart", help="resume an agent's conversation in its worktree")
    s.add_argument("name")
    s.set_defaults(func=cmd_restart)
    s = sub.add_parser("attach", help="take over the terminal of an agent (Ctrl+] detaches)")
    s.add_argument("name")
    s.set_defaults(func=cmd_attach)
    s = sub.add_parser("peek", help="print an agent's current screen")
    s.add_argument("name")
    s.add_argument("-n", "--lines", type=int, default=30)
    s.set_defaults(func=cmd_peek)
    for alias in ("rm", "delete"):
        s = sub.add_parser(alias, help="delete an agent's worktree and branch")
        s.add_argument("name")
        s.add_argument("--yes", metavar="NAME", help="confirm losing unmerged work by repeating the name")
        s.set_defaults(func=cmd_rm)
    s = sub.add_parser("adopt", help="track an untracked worktree under the worktree dir")
    s.add_argument("name")
    s.set_defaults(func=cmd_adopt)
    s = sub.add_parser("send", help="type a one-line message into an agent")
    s.add_argument("name")
    s.add_argument("text", nargs="+")
    s.set_defaults(func=cmd_send)
    s = sub.add_parser("broadcast", help="type a one-line message into every running agent")
    s.add_argument("text", nargs="+")
    s.set_defaults(func=cmd_broadcast)
    s = sub.add_parser("hook", help=argparse.SUPPRESS)
    s.add_argument("event")
    s.set_defaults(func=cmd_hook)
    s = sub.add_parser("dash", help="open the dashboard (same as plain `cc`)")
    s.set_defaults(func=cmd_dash)
    s = sub.add_parser("version")
    s.set_defaults(func=lambda ns: (_out(__import__("ccdash").__version__), 0)[1])
    return p


def build_task_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cc", description="Start an agent for a task.")
    p.add_argument("task", nargs="+")
    p.add_argument("--name")
    p.add_argument("--base", help="branch to start from (default: the default branch; HEAD for the main checkout)")
    p.add_argument("--model")
    p.add_argument("--permission-mode")
    p.set_defaults(func=cmd_new)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    first = next((a for a in argv if not a.startswith("-")), None)
    if first is not None and first not in SUBCOMMANDS:
        parser = build_task_parser()
    else:
        parser = build_parser()
    if argv and argv[0] == "help":
        build_parser().print_help()
        return 0
    ns = parser.parse_args(argv)
    if getattr(ns, "cmd", None) is None and not hasattr(ns, "task"):
        ns.func = cmd_dash
    if not hasattr(ns, "no_anim"):
        ns.no_anim = False
    from .repo import RepoError
    from .workspace import WorkspaceError

    try:
        return int(ns.func(ns) or 0)
    except (RepoError, WorkspaceError) as exc:
        _err(str(exc))
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        from .config import ConfigError
        from .gitutil import GitError

        if isinstance(exc, (ConfigError, GitError, RuntimeError)):
            _err(str(exc))
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
