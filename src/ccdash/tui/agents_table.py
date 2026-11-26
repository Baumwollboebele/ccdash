"""The agent table: one row per agent, updated cell by cell."""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable

from ..workspace import AgentView
from .theme import ACCENT, DIM, SPINNER, STATE_STYLE, STATUS_GLYPH, STATUS_LABEL, STATUS_STYLE

COLUMNS = [("name", "NAME", 20), ("status", "STATUS", 14), ("task", "TASK", 28), ("branch", "BRANCH", 20),
           ("main", "↑↓ MAIN", 9), ("files", "FILES", 6), ("state", "STATE", 9)]
FIXED = sum(w for k, _, w in COLUMNS if k != "task")


def progress_bar(frac: float, width: int = 4) -> str:
    full = max(0, min(width, round(frac * width)))
    return "▰" * full + "▱" * (width - full)


def status_cell(v: AgentView, glyph: str | None = None, pulse: str | None = None, progress=None, **_) -> Text:
    style = STATUS_STYLE.get(v.status, "default")
    label = STATUS_LABEL.get(v.status, v.status)
    if progress is not None and v.status in ("setup", "starting"):
        return Text.assemble((progress_bar(progress.frac), ACCENT), " ", (progress.step, ACCENT))
    if v.stale:
        style = DIM
    if pulse and v.status == "needs_input":
        style = f"bold {pulse}"
    return Text.assemble((glyph or STATUS_GLYPH.get(v.status, " "), style), " ", (label, style))

def state_cell(v: AgentView, fade: str | None = None) -> Text:
    s = v.merge_state
    if fade and s == "merged":
        return Text("merged", style=fade)
    if s == "committed":
        return Text(f"+{v.ahead}")
    if s == "dirty":
        return Text(f"dirty {v.dirty}" if v.dirty else "dirty")
    if s == "conflict":
        return Text("⚠ conf", style="bold red")
    if s.startswith("PR"):
        return Text(s, style="cyan")
    return Text(s, style=STATE_STYLE.get(s, "default"))


def main_cell(v: AgentView, flash: bool = False) -> Text:
    if not v.branch:
        return Text("")
    t = Text()
    t.append(f"+{v.ahead}", style=f"bold {ACCENT}" if flash else ("green" if v.ahead else DIM))
    t.append(" ")
    t.append(f"−{v.behind}", style="yellow" if v.behind > 20 else (DIM if not v.behind else "default"))
    return t


class AgentsTable(DataTable):
    BINDINGS = [Binding("enter", "app.attach", "attach", show=False)]

    def __init__(self, **kwargs):
        super().__init__(cursor_type="row", zebra_stripes=False, show_cursor=False, **kwargs)  # selection = ▶ + bold
        self.task_width = 28
        self._order: list[str] = []

    def on_mount(self) -> None:
        for key, label, width in COLUMNS:
            self.add_column(Text(label), key=key, width=width)

    def fit_width(self, total: int) -> None:
        task_w = max(12, total - FIXED - 2 * len(COLUMNS) - 6)
        if task_w != self.task_width:
            self.task_width = task_w
            self.columns["task"].width = task_w
            self.refresh()

    def selected(self) -> str | None:
        if not self.row_count:
            return None
        try:
            key = self.coordinate_to_cell_key(self.cursor_coordinate).row_key
        except Exception:  # noqa: BLE001
            return None
        return str(key.value) if key is not None else None

    def select(self, name: str) -> None:
        if name in self._order:
            self.move_cursor(row=self._order.index(name))

    def render_row(self, v: AgentView, selected: bool, glyph=None, pulse=None, progress=None, flash=False,
                   fade=None, **_) -> dict:
        marker = Text("▶ ", style=ACCENT) if selected else Text("  ")
        name_style = "bold" if selected else ("dim" if v.status in ("stopped", "deleted", "untracked") else "default")
        if fade:
            name_style = fade
        elif pulse and v.status == "needs_input":
            name_style = f"bold {pulse}"
        task = (v.task.splitlines() or [""])[0]
        if len(task) > self.task_width:
            task = task[: self.task_width - 1] + "…"
        return {
            "name": marker + Text(v.name, style=name_style),
            "status": status_cell(v, glyph=glyph, pulse=pulse, progress=progress),
            "task": Text(task, style=DIM if v.status == "untracked" else (fade or "default")),
            "branch": Text(v.branch, style=DIM),
            "main": main_cell(v, flash=flash),
            "files": Text(str(v.files) if v.branch else "", style="default" if v.files else DIM),
            "state": state_cell(v, fade=fade),
        }

    def refresh_rows(self, views: list[AgentView], status_kw_for, columns=("name", "status", "main", "state")) -> None:
        """Animation frame: re-render only the given rows' animated cells."""
        sel = self.selected()
        for v in views:
            if v.name not in self._order:
                continue
            cells = self.render_row(v, selected=v.name == sel, **status_kw_for(v))
            for key in columns:
                try:
                    if self.get_cell(v.name, key) != cells[key]:
                        self.update_cell(v.name, key, cells[key])
                except Exception:  # noqa: BLE001
                    pass
    def update_views(self, views: list[AgentView], status_kw_for) -> None:
        """Re-render rows; rebuild only when the set or order of agents changed."""
        names = [v.name for v in views]
        keep = self.selected()
        if names != self._order:
            self.clear()
            self._order = names
            for v in views:
                cells = self.render_row(v, selected=False, **status_kw_for(v))
                self.add_row(*(cells[k] for k, _, _ in COLUMNS), key=v.name)
            if keep in names:
                self.move_cursor(row=names.index(keep), animate=False)
        sel = self.selected()
        for v in views:
            cells = self.render_row(v, selected=v.name == sel, **status_kw_for(v))
            for key, _, _ in COLUMNS:
                try:
                    if self.get_cell(v.name, key) != cells[key]:
                        self.update_cell(v.name, key, cells[key])
                except Exception:  # noqa: BLE001  (row vanished mid-update)
                    pass

    def row_plain(self, name: str) -> str:
        try:
            return " ".join(str(c) if not isinstance(c, Text) else c.plain for c in self.get_row(name))
        except Exception:  # noqa: BLE001
            return ""
