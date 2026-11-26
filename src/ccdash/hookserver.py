"""The host's localhost endpoint for HTTP hooks (Windows).

Claude POSTs each hook event to http://127.0.0.1:<port>/hook/<token>/<Event>; the
body is the same JSON a command hook gets on stdin, and the response body is read
the same way as a command hook's stdout ({} = no decision).
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import hook, hooks_settings


class HookServer:
    def __init__(self, name: str, state_dir: Path, worktree: Path, decide=None):
        self.name = name
        self.state_dir = str(state_dir)
        self.worktree = str(worktree)
        self.token = secrets.token_hex(16)
        self.decide = decide  # guard mode: (line) -> dict | None
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                parts = self.path.strip("/").split("/")
                if len(parts) != 3 or parts[0] != "hook" or parts[1] != server.token:
                    self.send_error(404)
                    return
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                out = b"{}"
                try:
                    line = hook.handle(parts[2], body, server.name, server.state_dir, server.worktree)
                    if server.decide is not None and line is not None and parts[2] == "PreToolUse":
                        decision = server.decide(line)
                        if decision:
                            out = json.dumps(decision).encode("utf-8")
                except Exception:  # noqa: BLE001  (never fail the agent's tool call)
                    out = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, name="hooks", daemon=True).start()

    def settings(self) -> dict:
        return hooks_settings.render_http(self.port, self.token)


def start(name: str, state_dir: Path, worktree: Path) -> HookServer:
    decide = None
    try:
        from .guard import decider_for

        decide = decider_for(state_dir, name)
    except ImportError:
        pass
    return HookServer(name, state_dir, worktree, decide=decide)
