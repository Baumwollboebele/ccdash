"""Look and feel: terminal-native colors, one warm accent, glyphs for states."""

from __future__ import annotations

import os

from textual.theme import Theme

ACCENT = "#D97757"  # warm coral, for focus and your own actions
DIM = "bright_black"  # Rich name: the terminal palette's grey, readable on light and dark themes

STATUS_STYLE = {
    "working": "green", "running": "green", "needs_input": "yellow", "finished": "blue", "idle": "default",
    "stopped": DIM, "stopping": DIM, "crashed": "red", "setup": ACCENT, "starting": ACCENT, "untracked": DIM, "deleted": DIM,
}
STATUS_GLYPH = {
    "working": "●", "running": "●", "needs_input": "◐", "finished": "✓", "idle": "○", "stopped": "■", "stopping": "□",
    "crashed": "✕", "setup": "◌", "starting": "◌", "untracked": "?", "deleted": "·",
}
STATUS_LABEL = {"needs_input": "needs input"}
STATE_STYLE = {"merged": "blue", "conflict": "red", "ready": "green", "dirty": "default", "committed": "default",
               "clean": DIM}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _theme(name: str, dark: bool, rgb: bool = False) -> Theme:
    """ANSI themes use the terminal's own background and palette (native in light and dark
    terminals). The -rgb variants use fixed colors: screenshots, and terminals without
    proper default colors."""
    if rgb:
        bg, fg, dim = ("#1b1b1b", "#d6d6d6", "#7a7a7a") if dark else ("#fbfaf8", "#262626", "#8c8c8c")
        black, white = ("#000000", "#ffffff")
    else:
        bg, fg, dim = "ansi_default", "ansi_default", "ansi_bright_black"
        black, white = "ansi_black", "ansi_white"
    return Theme(
        name=name,
        primary=ACCENT,
        secondary="ansi_cyan",
        accent=ACCENT,
        warning="ansi_yellow",
        error="ansi_red",
        success="ansi_green",
        foreground=fg,
        background=bg,
        surface=bg,
        panel=bg,
        boost=bg,
        dark=dark,
        ansi=not rgb,
        variables={
            "ansi-background": black if dark else white,
            "ansi-foreground": white if dark else black,
            "ccdash-bg": bg,
            "ccdash-dim": dim,
            "border": ACCENT,
            "border-blurred": dim,
            "block-cursor-foreground": fg,
            "block-cursor-background": bg,
            "block-cursor-text-style": "bold",
            "block-cursor-blurred-text-style": "bold",
            "input-cursor-background": ACCENT,
            "input-cursor-foreground": black,
            "input-selection-background": dim,
            "footer-key-foreground": ACCENT,
            "scrollbar": dim,
            "scrollbar-background": bg,
            "scrollbar-corner-color": bg,
        },
    )


DARK = _theme("ccdash-dark", True)
LIGHT = _theme("ccdash-light", False)
DARK_RGB = _theme("ccdash-dark-rgb", True, rgb=True)
LIGHT_RGB = _theme("ccdash-light-rgb", False, rgb=True)
THEMES = (DARK, LIGHT, DARK_RGB, LIGHT_RGB)


def pick_theme() -> str:
    """ccdash-light when the terminal says it has a light background (COLORFGBG), else dark.
    CCDASH_THEME = dark | light | dark-rgb | light-rgb forces one."""
    forced = os.environ.get("CCDASH_THEME")
    if forced in ("light", "dark", "dark-rgb", "light-rgb"):
        return f"ccdash-{forced}"
    fgbg = os.environ.get("COLORFGBG", "")
    try:
        bg = int(fgbg.split(";")[-1])
        return "ccdash-light" if bg in (7, 15) else "ccdash-dark"
    except ValueError:
        return "ccdash-dark"


def reduced_motion(no_anim: bool = False) -> bool:
    return no_anim or bool(os.environ.get("NO_COLOR")) or os.environ.get("CCDASH_REDUCED_MOTION") == "1"


def fmt_clock(ts: float) -> str:
    """HH:MM for a timestamp; one place, so snapshot tests can freeze it."""
    import time

    return time.strftime("%H:%M", time.localtime(ts))
