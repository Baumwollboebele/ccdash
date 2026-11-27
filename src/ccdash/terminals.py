"""Open an agent in a new terminal tab instead of taking over the dashboard's terminal
(`open_in` in the config; off by default).

  wt      Windows Terminal: `wt.exe -w 0 new-tab --title <name> -d <repo> <python> -m ccdash attach <name>`
          (-w 0 = the window the dashboard runs in).
  vscode  Not possible: VS Code has no command line that opens a tab in the integrated
          terminal of a running window (`code --new-window` opens an empty editor window,
          which helps nobody). ccdash says so once and attaches in place.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

VSCODE_NOTE = ("VS Code has no command line for opening a terminal tab, so open_in = \"vscode\" attaches here. "
               "Split the terminal yourself and run: cc attach {name}")


def attach_argv(name: str) -> list[str]:
    """`cc attach <name>` through the interpreter this dashboard runs on (cc may not be on PATH in a new tab)."""
    return [sys.executable, "-m", "ccdash", "attach", name]


def new_tab_command(open_in: str, name: str, cwd: Path | str) -> list[str] | None:
    """argv that opens `cc attach <name>` in a new tab, or None when that is not possible here."""
    if open_in != "wt":
        return None
    wt = shutil.which("wt") or shutil.which("wt.exe")
    if wt is None:
        return None
    return [wt, "-w", "0", "new-tab", "--title", name, "-d", str(cwd), *attach_argv(name)]


def spawn(argv: list[str]) -> None:
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW: wt opens its own tab
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)


def open_tab(open_in: str, name: str, cwd: Path | str) -> bool:
    argv = new_tab_command(open_in, name, cwd)
    if argv is None:
        return False
    spawn(argv)
    return True
