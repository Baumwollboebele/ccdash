"""Post-Phase-0 spike: do Claude Code HTTP hooks work on native Windows, and how fast?

    python httphook.py <rundir>      # serves until killed; writes <rundir>/http.port and http-events.jsonl

Writes <rundir>/http-settings.json with `type: "http"` hooks for the main events.
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EVENTS = ["SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
          "PermissionRequest", "Notification", "Stop"]

rundir = os.path.abspath(sys.argv[1])
os.makedirs(rundir, exist_ok=True)
log_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        t = time.time()
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            data = json.loads(body)
        except ValueError:
            data = body.decode("utf-8", "replace")
        rec = {"recv": t, "path": self.path, "headers": dict(self.headers), "input": data}
        with log_lock, open(os.path.join(rundir, "http-events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        out = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
settings = {"hooks": {ev: [{"hooks": [{"type": "http", "url": f"http://127.0.0.1:{port}/hook/p0/{ev}", "timeout": 5}]}]
                      for ev in EVENTS}}
with open(os.path.join(rundir, "http-settings.json"), "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2)
with open(os.path.join(rundir, "http.port"), "w") as f:
    f.write(str(port))
srv.serve_forever()
