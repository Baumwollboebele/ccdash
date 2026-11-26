"""Phase 0 spike: hook logger.

Usage (as a Claude Code hook command):  python hooklog.py <label>
Appends one JSON line per call to $P0_LOG (or ./p0-events.jsonl).
Records the hook stdin, selected env vars, the parent process chain (to see
which shell, if any, ran the hook) and the in-process time.
Never writes to stdout, always exits 0.
"""

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

T0 = time.perf_counter()


def process_table():
    """pid -> (ppid, exe name) via Toolhelp32 snapshot (Windows only)."""
    if os.name != "nt":
        return {}

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    table = {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return table


def parent_chain(depth=5):
    table = process_table()
    chain, pid = [], os.getpid()
    for _ in range(depth):
        if pid not in table:
            break
        ppid, exe = table[pid]
        chain.append(f"{exe}:{pid}")
        pid = ppid
    return chain


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        raw = f"<stdin error {exc}>"
    try:
        data = json.loads(raw) if raw.strip() else None
    except ValueError:
        data = None
    t_read = (time.perf_counter() - T0) * 1000
    env = {
        k: ("<set>" if "TOKEN" in k else v)
        for k, v in os.environ.items()
        if k.startswith(("CCDASH", "CLAUDE", "P0_")) or k in ("SHELL", "MSYSTEM", "COMSPEC", "TERM", "COLORTERM", "WT_SESSION")
    }
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}",
        "label": label,
        "argv": sys.argv,
        "cwd": os.getcwd(),
        "chain": parent_chain(),
        "env": env,
        "ms_to_stdin": round(t_read, 1),
        "input": data if data is not None else raw[:4000],
    }
    rec["ms_total"] = round((time.perf_counter() - T0) * 1000, 1)
    path = os.environ.get("P0_LOG") or os.path.join(os.getcwd(), "p0-events.jsonl")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    try:
        main()
    finally:
        os._exit(0)
