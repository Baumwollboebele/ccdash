"""`cc attach <name>` on Windows: proxy the console to an agent's host.

The console is put in raw VT mode; keys go to the agent's ConPTY through the host
pipe and its output comes back. F12 detaches, and so does the key that types the
character 0x1D (Ctrl+] on a US keyboard, Ctrl++ on a German one). The agent keeps running.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from typing import Callable

DETACH_KEY = "\x1d"  # the character Ctrl+] types on a US keyboard; on a German one it is Ctrl++
_F12 = re.compile(r"\x1b\[24(?:;\d+)?~")  # F12 (with any modifier) as a VT sequence: the same key on every layout
_F12_PARTS = ("\x1b", "\x1b[", "\x1b[2", "\x1b[24", "\x1b[24;")


def detach_key_name() -> str:
    """The key that types the detach character on this keyboard layout: `Ctrl+]` on a US
    layout, `Ctrl++` on a German one (where `]` needs AltGr and Ctrl+AltGr+9 types nothing)."""
    if sys.platform != "win32":
        return "Ctrl+]"
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32")
        user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
        user32.VkKeyScanW.restype = ctypes.c_short
        user32.GetKeyNameTextW.argtypes = [ctypes.c_long, ctypes.c_wchar_p, ctypes.c_int]
        found = user32.VkKeyScanW(DETACH_KEY)
        vk, shift = found & 0xFF, (found >> 8) & 0xFF
        if found == -1 or shift != 2:  # not typed by Ctrl plus one key here
            return "Ctrl+]"
        name = ctypes.create_unicode_buffer(32)
        user32.GetKeyNameTextW(user32.MapVirtualKeyW(vk, 0) << 16, name, 32)
        return f"Ctrl+{name.value}" if name.value else "Ctrl+]"
    except Exception:  # noqa: BLE001  (a hint must never break attach)
        return "Ctrl+]"


def detach_hint() -> str:
    return f"F12 or {detach_key_name()}"


class DetachScanner:
    """Finds the detach keys in what you type. F12 arrives as several characters, which may come in
    two reads: a tail that could be the start of F12 is held back until the next call says what follows."""

    def __init__(self):
        self.held = ""

    def feed(self, text: str) -> tuple[str, bool]:
        """(what to pass on to the agent, whether to detach now). An empty `text` releases what was held."""
        text, self.held = self.held + text, ""
        if DETACH_KEY in text:
            return text.split(DETACH_KEY, 1)[0], True
        m = _F12.search(text)
        if m:
            return text[: m.start()], True
        for part in reversed(_F12_PARTS):  # longest first
            if text.endswith(part):
                self.held = part
                return text[: -len(part)], False
        return text, False

    def flush(self) -> str:
        text, self.held = self.held, ""
        return text


class AttachSession:
    """Pipe-level attach: send input bytes, receive output via a callback (used by tests and the console proxy)."""

    def __init__(self, info: dict, rows: int, cols: int, on_output: Callable[[str], None],
                 on_exit: Callable[[dict], None] | None = None):
        from . import pipeproto

        self.conn = pipeproto.connect(info)
        self.on_output = on_output
        self.on_exit = on_exit
        self.closed = threading.Event()
        self.conn.send_bytes(json.dumps({"op": "attach", "rows": rows, "cols": cols}).encode())
        # The first frame is the repaint of the current screen; deliver it before returning.
        if self.conn.poll(5):
            self._dispatch(self.conn.recv_bytes())
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _dispatch(self, frame: bytes) -> None:
        kind, body = frame[:1], frame[1:]
        if kind == b"O":
            self.on_output(body.decode("utf-8", "replace"))
        elif kind == b"X":
            self.closed.set()
            if self.on_exit:
                self.on_exit(json.loads(body or b"{}"))

    def _pump(self) -> None:
        try:
            while not self.closed.is_set():
                self._dispatch(self.conn.recv_bytes())
        except (EOFError, OSError):
            self.closed.set()
            if self.on_exit:
                self.on_exit({})

    def send_input(self, data: bytes) -> None:
        if not self.closed.is_set():
            self.conn.send_bytes(b"I" + data)

    def resize(self, rows: int, cols: int) -> None:
        if not self.closed.is_set():
            self.conn.send_bytes(b"R" + json.dumps({"rows": rows, "cols": cols}).encode())

    def detach(self) -> None:
        if not self.closed.is_set():
            try:
                self.conn.send_bytes(b"D")
            except OSError:
                pass
        self.closed.set()
        try:
            self.conn.close()
        except OSError:
            pass

    def __enter__(self) -> "AttachSession":
        return self

    def __exit__(self, *exc) -> None:
        self.detach()


# -- Windows console proxy ------------------------------------------------------------
def run_console(info: dict, title: str = "") -> int:
    if sys.platform != "win32":
        raise RuntimeError("the console attach proxy is for Windows; tmux attaches natively elsewhere")
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    STD_INPUT, STD_OUTPUT = -10, -11
    ENABLE_PROCESSED_INPUT, ENABLE_LINE_INPUT, ENABLE_ECHO_INPUT = 0x1, 0x2, 0x4
    ENABLE_WINDOW_INPUT, ENABLE_MOUSE_INPUT, ENABLE_QUICK_EDIT = 0x8, 0x10, 0x40
    ENABLE_EXTENDED_FLAGS, ENABLE_VT_INPUT = 0x80, 0x200
    ENABLE_PROCESSED_OUTPUT, ENABLE_VT_PROCESSING, DISABLE_NEWLINE_AUTO_RETURN = 0x1, 0x4, 0x8
    k32.GetStdHandle.restype = wintypes.HANDLE
    hin, hout = k32.GetStdHandle(STD_INPUT), k32.GetStdHandle(STD_OUTPUT)

    class COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class SMALL_RECT(ctypes.Structure):
        _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short), ("Right", ctypes.c_short),
                    ("Bottom", ctypes.c_short)]

    class CSBI(ctypes.Structure):
        _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD), ("wAttributes", wintypes.WORD),
                    ("srWindow", SMALL_RECT), ("dwMaximumWindowSize", COORD)]

    class KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD), ("wVirtualKeyCode", wintypes.WORD),
                    ("wVirtualScanCode", wintypes.WORD), ("UnicodeChar", wintypes.WCHAR),
                    ("dwControlKeyState", wintypes.DWORD)]

    class _EVENT(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_pad", ctypes.c_byte * 16)]

    class INPUT_RECORD(ctypes.Structure):
        _fields_ = [("EventType", wintypes.WORD), ("Event", _EVENT)]

    def size() -> tuple[int, int]:
        csbi = CSBI()
        if not k32.GetConsoleScreenBufferInfo(hout, ctypes.byref(csbi)):
            return 35, 120
        w = csbi.srWindow
        return w.Bottom - w.Top + 1, w.Right - w.Left + 1

    in_mode, out_mode = wintypes.DWORD(), wintypes.DWORD()
    if not k32.GetConsoleMode(hin, ctypes.byref(in_mode)) or not k32.GetConsoleMode(hout, ctypes.byref(out_mode)):
        raise RuntimeError("cc attach needs an interactive console (Windows Terminal, VS Code or conhost)")
    in_cp, out_cp = k32.GetConsoleCP(), k32.GetConsoleOutputCP()

    out = sys.stdout

    def write(text: str) -> None:
        out.write(text)
        out.flush()

    rows, cols = size()
    done = threading.Event()
    result = {"code": 0}

    def on_exit(ex: dict) -> None:
        result["code"] = int(ex.get("exitstatus") or 0) if ex else 0
        done.set()

    raw_in = (ENABLE_VT_INPUT | ENABLE_EXTENDED_FLAGS) & ~(ENABLE_PROCESSED_INPUT | ENABLE_LINE_INPUT |
                                                            ENABLE_ECHO_INPUT | ENABLE_QUICK_EDIT)
    k32.SetConsoleMode(hin, raw_in)
    k32.SetConsoleMode(hout, out_mode.value | ENABLE_PROCESSED_OUTPUT | ENABLE_VT_PROCESSING |
                       DISABLE_NEWLINE_AUTO_RETURN)
    k32.SetConsoleCP(65001)
    k32.SetConsoleOutputCP(65001)
    write("\x1b[?1049h")  # alternate screen: detaching gives the shell its screen back
    if title:  # the agent's screen fills the terminal, so the way back is in the window title
        write(f"\x1b]0;cc · {title} · {detach_hint()} goes back\x07")
    session = None
    scanner = DetachScanner()
    try:
        session = AttachSession(info, rows, cols, on_output=write, on_exit=on_exit)

        def watch_size() -> None:
            last = (rows, cols)
            while not done.is_set():
                time.sleep(0.25)
                cur = size()
                if cur != last:
                    last = cur
                    session.resize(*cur)

        threading.Thread(target=watch_size, daemon=True).start()
        buf = (INPUT_RECORD * 64)()
        n = wintypes.DWORD()
        pending_high = None
        while not done.is_set():
            if k32.WaitForSingleObject(hin, 100) != 0:
                held = scanner.flush()  # a lone Esc is not the start of F12 after all
                if held:
                    session.send_input(held.encode("utf-8"))
                continue
            if not k32.ReadConsoleInputW(hin, buf, 64, ctypes.byref(n)):
                break
            chars = []
            for i in range(n.value):
                rec = buf[i]
                if rec.EventType != 1 or not rec.Event.KeyEvent.bKeyDown:
                    continue
                ch = rec.Event.KeyEvent.UnicodeChar
                if not ch:
                    continue
                code = ord(ch)
                if 0xD800 <= code < 0xDC00:
                    pending_high = ch
                    continue
                if 0xDC00 <= code < 0xE000 and pending_high:
                    ch = (pending_high + ch).encode("utf-16", "surrogatepass").decode("utf-16")
                    pending_high = None
                chars.append(ch * max(1, rec.Event.KeyEvent.wRepeatCount))
            if not chars:
                continue
            text, detach = scanner.feed("".join(chars))
            if text:
                session.send_input(text.encode("utf-8"))
            if detach:
                break
    finally:
        if session is not None:
            session.detach()
        write("\x1b[?9001l\x1b[?1004l\x1b[?2004l\x1b[0m\x1b[?25h\x1b[?1049l")
        k32.SetConsoleMode(hin, in_mode.value)
        k32.SetConsoleMode(hout, out_mode.value)
        k32.SetConsoleCP(in_cp)
        k32.SetConsoleOutputCP(out_cp)
    return result["code"]
