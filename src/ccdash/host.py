"""`ccdash host`: keeps one agent's interactive claude alive in a ConPTY (Windows).

Started detached (twice removed from the TUI, so closing the terminal or killing its
process tree does not reach it). It owns:
  * the ConPTY running claude (pywinpty),
  * a VT screen emulator (pyte) for peek and for repainting on attach,
  * a named pipe for requests and attach streams (pipeproto),
  * optionally a localhost HTTP endpoint that receives the agent's hook events.
Files in <state>/run/: <name>.host.json while running, <name>.exit.json after the agent exits.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path

ATTACH_FILTER = re.compile(r"\x1b\[\?(?:9001|1004)[hl]")  # win32-input-mode / focus events: keep out of the user's terminal


def launch() -> None:
    """Intermediate process: start the real host detached, then exit at once."""
    from .runner import pyboot, spawn_detached

    spec_path = sys.argv[1]
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    log = Path(spec["state_dir"]) / "logs" / f"{spec['name']}.host.log"
    spawn_detached(pyboot("ccdash.host", "main", [spec_path]), log_path=log)


def main() -> None:
    spec_path = Path(sys.argv[1])
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    run_dir = Path(spec["state_dir"]) / "run"
    try:
        Host(spec).run()
    except Exception:  # noqa: BLE001
        err = traceback.format_exc()
        (run_dir / f"{spec['name']}.error.json").write_text(json.dumps({"error": err}), encoding="utf-8")
        sys.stderr.write(err)
        os._exit(1)


# -- pyte helpers -------------------------------------------------------------------
_NAMES = {"black": 0, "red": 1, "green": 2, "brown": 3, "blue": 4, "magenta": 5, "cyan": 6, "white": 7}


def _sgr_color(color: str, fg: bool) -> str:
    if color == "default":
        return "39" if fg else "49"
    base = 30 if fg else 40
    if color in _NAMES:
        return str(base + _NAMES[color])
    if color.startswith("bright") and color[6:] in _NAMES:
        return str(base + 60 + _NAMES[color[6:]])
    if len(color) == 6:
        try:
            r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
            return f"{38 if fg else 48};2;{r};{g};{b}"
        except ValueError:
            pass
    return "39" if fg else "49"


def render_ansi(screen) -> str:
    """Repaint the whole emulated screen (with colors) for a newly attached terminal."""
    out = ["\x1b[0m\x1b[2J\x1b[H"]
    for y in range(screen.lines):
        row = screen.buffer[y]
        last = None
        line = []
        for x in range(screen.columns):
            ch = row[x]
            attrs = (ch.fg, ch.bg, ch.bold, ch.italics, ch.underscore, ch.reverse)
            if attrs != last:
                codes = ["0", _sgr_color(ch.fg, True), _sgr_color(ch.bg, False)]
                if ch.bold:
                    codes.append("1")
                if ch.italics:
                    codes.append("3")
                if ch.underscore:
                    codes.append("4")
                if ch.reverse:
                    codes.append("7")
                line.append(f"\x1b[{';'.join(codes)}m")
                last = attrs
            line.append(ch.data)
        out.append("".join(line).rstrip() + "\x1b[0m")
        out.append("\r\n" if y < screen.lines - 1 else "")
    out.append(f"\x1b[{screen.cursor.y + 1};{screen.cursor.x + 1}H")
    return "".join(out)


def _line_text(line, columns: int) -> str:
    return "".join(line[x].data for x in range(columns)).rstrip()


class Host:
    def __init__(self, spec: dict):
        self.spec = spec
        self.name = spec["name"]
        self.state_dir = Path(spec["state_dir"])
        self.run_dir = self.state_dir / "run"
        self.write_lock = threading.Lock()
        self.screen_lock = threading.Lock()
        self.clients: list = []
        self.clients_lock = threading.Lock()
        self.exited = threading.Event()
        self.killed = False
        self.hook_server = None
        self.proc = None

    # -- startup --------------------------------------------------------------
    def run(self) -> None:
        import pyte
        from multiprocessing.connection import Listener

        from winpty import PtyProcess

        from .runner import process_created
        from .state import atomic_write_json

        spec = self.spec
        env = {k: v for k, v in os.environ.items() if k not in set(spec.get("env_unset", []))}
        env.update(spec.get("env_set", {}))
        args = list(spec["args"])
        port = None
        if spec.get("settings") == "http":
            from . import hookserver

            self.hook_server = hookserver.start(self.name, self.state_dir, Path(spec["worktree"]))
            port = self.hook_server.port
            settings_path = self.run_dir / f"{self.name}.settings.json"
            atomic_write_json(settings_path, self.hook_server.settings())
            args = ["--settings", str(settings_path), *args]

        rows, cols = int(spec.get("rows", 35)), int(spec.get("cols", 120))
        self.screen = pyte.HistoryScreen(cols, rows, history=2000, ratio=0.5)
        self.screen.write_process_input = lambda data: None  # never answer terminal queries ourselves
        self.stream = pyte.Stream(self.screen)
        self.listener = Listener(spec["pipe"], family="AF_PIPE", authkey=bytes.fromhex(spec["authkey"]))
        self.proc = PtyProcess.spawn([*spec["cmd"], *args], cwd=spec["cwd"], env=env, dimensions=(rows, cols))

        info = {
            "name": self.name,
            "host_pid": os.getpid(),
            "host_created": process_created(os.getpid()),
            "claude_pid": self.proc.pid,
            "pipe": spec["pipe"],
            "authkey": spec["authkey"],
            "hook_port": port,
            "started": time.time(),
        }
        threading.Thread(target=self._reader, name="reader", daemon=True).start()
        threading.Thread(target=self._serve, name="pipe", daemon=True).start()
        atomic_write_json(self.run_dir / f"{self.name}.host.json", info)

        while not self.exited.wait(0.25):
            if not self.proc.isalive():
                time.sleep(0.2)  # let the reader drain the last output
                break
        self._finish()

    def _finish(self) -> None:
        from .state import State, atomic_write_json

        status = self.proc.exitstatus if self.proc is not None else None
        ex = {"exitstatus": status if status is not None else -1, "ended": time.time(), "killed": self.killed}
        atomic_write_json(self.run_dir / f"{self.name}.exit.json", ex)
        try:
            State(self.state_dir).append_event({"agent": self.name, "event": "ccdash.exit", **ex})
        except Exception:  # noqa: BLE001
            pass
        self._broadcast(b"X" + json.dumps(ex).encode())
        time.sleep(0.3)
        os._exit(0)

    # -- terminal I/O -----------------------------------------------------------
    def _reader(self) -> None:
        while True:
            try:
                data = self.proc.read(65536)
            except EOFError:
                break
            except Exception:  # noqa: BLE001
                break
            if not data:
                continue
            with self.screen_lock:
                try:
                    self.stream.feed(data)
                except Exception:  # noqa: BLE001  (an odd sequence must not kill the host)
                    pass
            if self.clients:
                out = ATTACH_FILTER.sub("", data)
                if out:
                    self._broadcast(b"O" + out.encode("utf-8", "replace"))
        self.exited.set()

    def _broadcast(self, frame: bytes) -> None:
        with self.clients_lock:
            clients = list(self.clients)
        for conn in clients:
            try:
                conn.send_bytes(frame)
            except Exception:  # noqa: BLE001
                with self.clients_lock:
                    if conn in self.clients:
                        self.clients.remove(conn)

    def write(self, data: str) -> None:
        with self.write_lock:
            if self.proc.isalive():
                self.proc.write(data)

    def type_text(self, text: str, enter: bool = True) -> None:
        text = " ".join(text.splitlines())  # one line: a newline would submit half a message
        self.write(text)
        if enter:
            time.sleep(0.15)  # sent separately, Enter submits instead of being pasted
            self.write("\r")

    def resize(self, rows: int, cols: int) -> None:
        rows, cols = max(5, rows), max(20, cols)
        with self.screen_lock:
            self.screen.resize(rows, cols)
        try:
            self.proc.setwinsize(rows, cols)
        except Exception:  # noqa: BLE001
            pass

    def graceful_stop(self, timeout: float = 10) -> None:
        """Esc, `/exit` Enter; then Ctrl+C twice; then kill. Worktree and branch are untouched."""
        deadline = time.monotonic() + timeout
        self.write("\x1b")
        time.sleep(0.3)
        self.write("/exit")
        time.sleep(0.3)
        self.write("\r")
        if self.exited.wait(max(0.5, min(5.0, deadline - time.monotonic()))):
            return
        self.write("\x03")
        time.sleep(0.25)
        self.write("\x03")
        if self.exited.wait(max(0.5, deadline - time.monotonic())):
            return
        self.kill()

    def kill(self) -> None:
        from .runner import kill_tree

        self.killed = True
        kill_tree(self.proc.pid)

    def screen_snapshot(self, history: int = 0) -> dict:
        with self.screen_lock:
            cols = self.screen.columns
            lines = [line.rstrip() for line in self.screen.display]
            hist = []
            if history:
                top = list(self.screen.history.top)[-history:]
                hist = [_line_text(line, cols) for line in top]
            return {"lines": lines, "history": hist, "cursor": [self.screen.cursor.y, self.screen.cursor.x],
                    "rows": self.screen.lines, "cols": cols}

    # -- pipe server --------------------------------------------------------------
    def _serve(self) -> None:
        while True:
            try:
                conn = self.listener.accept()
            except Exception:  # noqa: BLE001  (failed handshake or listener closed)
                if self.exited.is_set():
                    return
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _reply(self, conn, data: dict) -> None:
        conn.send_bytes(json.dumps(data).encode("utf-8"))

    def _handle(self, conn) -> None:
        try:
            while True:
                req = json.loads(conn.recv_bytes().decode("utf-8"))
                op = req.get("op")
                if op == "status":
                    self._reply(conn, {"alive": self.proc.isalive(), "claude_pid": self.proc.pid,
                                       "host_pid": os.getpid()})
                elif op == "screen":
                    self._reply(conn, self.screen_snapshot(int(req.get("history", 0))))
                elif op == "write":
                    self.write(req["data"])
                    self._reply(conn, {"ok": True})
                elif op == "type":
                    self.type_text(req["text"], bool(req.get("enter", True)))
                    self._reply(conn, {"ok": True})
                elif op == "resize":
                    self.resize(int(req["rows"]), int(req["cols"]))
                    self._reply(conn, {"ok": True})
                elif op == "stop":
                    threading.Thread(target=self.graceful_stop, args=(float(req.get("timeout", 10)),),
                                     daemon=True).start()
                    self._reply(conn, {"ok": True})
                elif op == "kill":
                    self._reply(conn, {"ok": True})
                    self.kill()
                elif op == "attach":
                    self._attach(conn, int(req.get("rows", 35)), int(req.get("cols", 120)))
                    return
                else:
                    self._reply(conn, {"error": f"unknown op {op!r}"})
        except (EOFError, OSError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _attach(self, conn, rows: int, cols: int) -> None:
        with self.screen_lock:
            paint = render_ansi(self.screen)
            same = (self.screen.lines, self.screen.columns) == (rows, cols)
        conn.send_bytes(b"O" + paint.encode("utf-8", "replace"))
        with self.clients_lock:
            self.clients.append(conn)
        # A size change makes ConPTY and claude repaint everything in the real terminal.
        if same:
            self.resize(rows, cols - 1)
            time.sleep(0.05)
        self.resize(rows, cols)
        try:
            while True:
                frame = conn.recv_bytes()
                kind, body = frame[:1], frame[1:]
                if kind == b"I":
                    self.write(body.decode("utf-8", "replace"))
                elif kind == b"R":
                    size = json.loads(body)
                    self.resize(int(size["rows"]), int(size["cols"]))
                elif kind == b"D":
                    break
        finally:
            with self.clients_lock:
                if conn in self.clients:
                    self.clients.remove(conn)
