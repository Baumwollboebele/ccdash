"""Network view (g) and Comms view (c)."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .. import comms
from .anim import lerp, spinner_glyph
from .canvas import BrailleCanvas
from . import theme
from .theme import ACCENT, DIM, STATUS_GLYPH, STATUS_STYLE

WINDOW = 900.0  # last 15 minutes
FLIGHT = 1.2  # seconds a message takes to fly along its edge
THREAD_STYLE = {"discussing": ACCENT, "agreed": "green", "acked": "blue", "open": "yellow", "delivered": DIM}


@dataclass
class Flight:
    sender: str
    to: str
    start: float

    def progress(self, now: float) -> float:
        return (now - self.start) / FLIGHT


def _edge_width(count: int) -> int:
    return 0 if count <= 0 else 1 if count <= 2 else 2 if count <= 5 else 3


def ellipse_layout(names: list[str], cols: int, rows: int, center: bool = True) -> dict[str, tuple[float, float]]:
    """Node centres in dot coordinates: around the middle, or on a full ellipse."""
    w, h = cols * 2, rows * 4
    cx, cy = w / 2, h / 2
    rx, ry = w * 0.36, h * 0.34
    pos = {}
    n = max(1, len(names))
    for i, name in enumerate(names):
        a = -math.pi / 2 + 2 * math.pi * i / n
        pos[name] = (cx + rx * math.cos(a), cy + ry * math.sin(a))
    return pos


def _label(canvas: BrailleCanvas, x: float, y: float, text: Text) -> None:
    col = int(x // 2) - len(text.plain) // 2
    row = int(y // 4)
    for i, (ch, style) in enumerate(_chars(text)):
        canvas.text(col + i, row, ch, style)


def _chars(text: Text):
    styles = [""] * len(text.plain)
    for span in text.spans:
        for i in range(span.start, min(span.end, len(styles))):
            styles[i] = str(span.style)
    base = str(text.style) if text.style else ""
    return [(ch, styles[i] or base) for i, ch in enumerate(text.plain)]


def branch_bar(ahead: int, behind: int) -> Text:
    blocks = min(6, ahead)
    t = Text()
    t.append(f"+{ahead} ", style=ACCENT if ahead else DIM)
    t.append(f"−{behind} ", style="yellow" if behind > 20 else DIM)
    t.append("▮" * blocks, style=ACCENT)
    return t


class NetworkView(Static):
    """Agents around `main`; edges are messages (thickness = count in the last 15 min),
    red edges are overlaps or predicted conflicts, each node has a tiny branch bar."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.flights: list[Flight] = []
        self._edges: dict[frozenset, tuple[str, int]] = {}
        self.last_text = Text()

    def edge_style(self, a: str, b: str) -> str | None:
        e = self._edges.get(frozenset({a, b}))
        return e[0] if e else None

    def edge_width(self, a: str, b: str) -> int:
        e = self._edges.get(frozenset({a, b}))
        return e[1] if e else 0

    def add_flight(self, sender: str, to: str) -> None:
        self.flights.append(Flight(sender, to, time.monotonic()))

    def show(self, views, messages, red_pairs: set, *, animate: bool, merged_progress=None) -> None:
        now_m, now = time.monotonic(), time.time()
        self.flights = [f for f in self.flights if f.progress(now_m) < 1.0]
        cols, rows = max(30, self.size.width or 60), max(8, self.size.height or 20)
        canvas = BrailleCanvas(cols, rows)
        agents = [v for v in views if v.status not in ("untracked", "deleted")]
        names = [v.name for v in agents]
        pos = ellipse_layout(names, cols, rows)
        main = (cols, rows * 2)
        for v in agents:
            p = merged_progress(v.name) if merged_progress else None
            if p is not None:  # a merged branch flows into main
                x, y = pos[v.name]
                pos[v.name] = (lerp(x, main[0], p), lerp(y, main[1], p))
        counts: dict[frozenset, int] = {}
        recent: set[frozenset] = set()
        for m in messages:
            if now - m.t > WINDOW:
                continue
            targets = m.recipients if m.to in ("all", "*") else [m.to]
            for to in targets:
                if m.sender in pos and to in pos and to != m.sender:
                    key = frozenset({m.sender, to})
                    counts[key] = counts.get(key, 0) + 1
                    if now - m.t < 120:
                        recent.add(key)
        self._edges = {}
        for v in agents:  # every branch hangs off main
            x, y = pos[v.name]
            style = "blue" if v.merge_state == "merged" else DIM
            canvas.line(x, y, *main, style=style, dotted=True, rank=0)
        for key, n in counts.items():
            a, b = sorted(key)
            style = "red" if key in red_pairs else (ACCENT if key in recent else "default")
            width = _edge_width(n)
            self._edges[key] = (style, width)
            canvas.line(*pos[a], *pos[b], style=style, width=width, rank=2 if style == "red" else 1)
        for key in red_pairs:
            if key in counts or len(key) != 2 or not all(k in pos for k in key):
                continue
            a, b = sorted(key)
            self._edges[key] = ("red", 1)
            canvas.line(*pos[a], *pos[b], style="red", width=1, rank=2)
        for f in self.flights:
            if f.sender in pos and f.to in pos:
                p = f.progress(now_m)
                (x0, y0), (x1, y1) = pos[f.sender], pos[f.to]
                canvas.text(int(lerp(x0, x1, p) // 2), int(lerp(y0, y1, p) // 4), "●", f"bold {ACCENT}")
        _label(canvas, *main, Text("◆ main", style=f"bold {ACCENT}"))
        for v in agents:
            x, y = pos[v.name]
            glyph = spinner_glyph() if animate and v.status == "working" else STATUS_GLYPH.get(v.status, " ")
            style = STATUS_STYLE.get(v.status, "default")
            _label(canvas, x, y, Text.assemble((glyph + " ", style), (v.name, "bold")))
            _label(canvas, x, y + 4, branch_bar(v.ahead, v.behind))
        self.last_text = canvas.render()
        self.update(self.last_text)


class CommsView(Vertical):
    """Full-screen agent-to-agent communication: graph, conversations, transcript, timeline."""

    def compose(self) -> ComposeResult:
        with Horizontal(id="comms-top"):
            yield Static(id="comms-graph", classes="panel")
            with Vertical(id="comms-right"):
                yield OptionList(id="comms-threads", classes="panel")
                yield Static(id="comms-transcript", classes="panel")
        yield Static(id="comms-timeline", classes="panel")

    def on_mount(self) -> None:
        self.query_one("#comms-graph").border_title = "Graph"
        self.query_one("#comms-threads").border_title = "Conversations"
        self.query_one("#comms-transcript").border_title = "Transcript"
        self.query_one("#comms-timeline").border_title = "Timeline · last 15 min"
        self.threads: list = []
        self._thread_sig = None
        self._timeline_text = Text()
        self._list_text = ""

    @property
    def selected(self):
        ol = self.query_one("#comms-threads", OptionList)
        if ol.highlighted is None or not self.threads or ol.highlighted >= len(self.threads):
            return None
        return self.threads[ol.highlighted]

    def show(self, views, messages, *, animate: bool, flights: list) -> None:
        now = time.time()
        self.threads = comms.build_threads(messages, now=now)
        self._render_list(animate)
        sel = self.selected
        self._render_graph(views, messages, sel, animate, flights, now)
        self._render_transcript(sel)
        self._render_timeline(views, messages, sel, now, animate)

    def _render_list(self, animate: bool) -> None:
        ol = self.query_one("#comms-threads", OptionList)
        lines = []
        for t in self.threads:
            state = t.state
            glyph = spinner_glyph() if (animate and state == "discussing") else {
                "agreed": "✓", "acked": "✓✓", "open": "○", "delivered": "→", "discussing": "●"}.get(state, " ")
            row = Text()
            row.append(f"{glyph:<2} ", style=THREAD_STYLE.get(state, "default"))
            row.append(f"{state:<10} ", style=THREAD_STYLE.get(state, "default"))
            row.append(t.pair_label, style="bold")
            row.append(f"  {t.topic}", style=DIM)
            row.append("  " + theme.fmt_clock(t.last_t), style=DIM)
            lines.append(row)
        sig = tuple(r.plain for r in lines)
        if sig != self._thread_sig:
            keep = ol.highlighted
            self._thread_sig = sig
            ol.clear_options()
            ol.add_options([Option(r) for r in lines] or [Option(Text("No conversations yet.", style=DIM),
                                                                  disabled=True)])
            if lines:
                ol.highlighted = min(keep or 0, len(lines) - 1)
        self._list_text = "\n".join(sig)

    def _render_graph(self, views, messages, sel, animate, flights, now) -> None:
        g = self.query_one("#comms-graph", Static)
        cols, rows = max(30, g.size.width - 4 or 50), max(6, g.size.height - 2 or 14)
        canvas = BrailleCanvas(cols, rows)
        names = [v.name for v in views if v.status not in ("untracked", "deleted")]
        for m in messages:
            for n in [m.sender, *(m.recipients if m.to in ("all", "*") else [m.to])]:
                if n not in names and n not in ("all", "*"):
                    names.append(n)
        for extra in ("you", "ccdash"):
            if extra not in names:
                names.append(extra)
        pos = ellipse_layout(names, cols, rows)
        state_of: dict[frozenset, str] = {}
        counts: dict[frozenset, int] = {}
        for t in reversed(self.threads):  # oldest first, newest state wins
            for m in t.messages:
                for to in (m.recipients if m.to in ("all", "*") else [m.to]):
                    key = frozenset({m.sender, to})
                    counts[key] = counts.get(key, 0) + 1
                    state_of[key] = t.state
        sel_keys = set()
        if sel is not None:
            for m in sel.messages:
                for to in (m.recipients if m.to in ("all", "*") else [m.to]):
                    sel_keys.add(frozenset({m.sender, to}))
        for key, n in counts.items():
            if len(key) != 2 or not all(k in pos for k in key):
                continue
            a, b = sorted(key)
            style = f"bold {ACCENT}" if key in sel_keys else THREAD_STYLE.get(state_of.get(key, "open"), DIM)
            canvas.line(*pos[a], *pos[b], style=style, width=_edge_width(n), rank=2 if key in sel_keys else 1)
            if animate and state_of.get(key) == "discussing":  # dots travel while a conversation is active
                p = (time.monotonic() / 1.5) % 1.0
                (x0, y0), (x1, y1) = pos[a], pos[b]
                canvas.text(int(lerp(x0, x1, p) // 2), int(lerp(y0, y1, p) // 4), "•", ACCENT)
        for f in flights:
            if f.sender in pos and f.to in pos:
                p = f.progress(time.monotonic())
                (x0, y0), (x1, y1) = pos[f.sender], pos[f.to]
                canvas.text(int(lerp(x0, x1, p) // 2), int(lerp(y0, y1, p) // 4), "●", f"bold {ACCENT}")
        for n in names:
            style = ACCENT if n == "you" else (DIM if n == "ccdash" else "bold")
            _label(canvas, *pos[n], Text(n, style=style))
        g.update(canvas.render())

    def _render_transcript(self, sel) -> None:
        tr = self.query_one("#comms-transcript", Static)
        if sel is None:
            tr.update(Text("Select a conversation.", style=DIM))
            return
        t = Text()
        for m in sel.messages:
            t.append(theme.fmt_clock(m.t) + " ", style=DIM)
            t.append(m.sender, style=ACCENT if m.kind == "you" else "bold")
            t.append(" → ", style=DIM)
            t.append(f"{m.to}\n")
            for line in m.text.splitlines()[:6]:
                t.append(f"  {line}\n")
        tr.update(t)

    def _render_timeline(self, views, messages, sel, now, animate) -> None:
        tl = self.query_one("#comms-timeline", Static)
        lanes = [v.name for v in views if v.status not in ("untracked", "deleted")]
        recent = [m for m in messages if now - m.t <= WINDOW]
        for m in recent:
            for n in [m.sender, *(m.recipients if m.to in ("all", "*") else [m.to])]:
                if n not in lanes and n not in ("all", "*"):
                    lanes.append(n)
        label_w = max([len(x) for x in lanes] + [6]) + 1
        width = max(20, (tl.size.width or 110) - 4 - label_w - 4)
        grid = [[" "] * width for _ in lanes]
        styles = [[DIM] * width for _ in lanes]
        sel_ids = {id(m) for m in sel.messages} if sel is not None else set()
        idx = {n: i for i, n in enumerate(lanes)}
        for lane in grid:
            for x in range(width):
                lane[x] = "·" if x % 4 == 0 else " "
        for m in recent:
            x = min(width - 1, int(width - (now - m.t) / WINDOW * width))
            if m.sender not in idx:
                continue
            style = f"bold {ACCENT}" if id(m) in sel_ids else ("default" if m.kind == "agent" else DIM)
            targets = [t for t in (m.recipients if m.to in ("all", "*") else [m.to]) if t in idx]
            s = idx[m.sender]
            rows_hit = [idx[t] for t in targets]
            lo, hi = min([s, *rows_hit]), max([s, *rows_hit])
            for r in range(lo, hi + 1):
                grid[r][x], styles[r][x] = "│", style
            grid[s][x], styles[s][x] = "●", style
            for r in rows_hit:
                if m.to in ("all", "*"):
                    grid[r][x], styles[r][x] = "•", style
                else:
                    grid[r][x], styles[r][x] = "▼" if r > s else "▲", style
        now_style = ACCENT if (not animate or int(time.monotonic() * 2) % 2 == 0) else DIM
        out = Text(no_wrap=True, overflow="crop")
        for i, name in enumerate(lanes):
            out.append(f"{name:>{label_w}} ", style="bold" if name not in ("you", "ccdash") else DIM)
            for x in range(width):
                out.append(grid[i][x], style=styles[i][x])
            out.append(" ┃", style=now_style)
            out.append("\n")
        out.append(" " * (label_w + 1) + "−15 min".ljust(width - 3) + "now", style=DIM)
        self._timeline_text = out
        tl.update(out)

    def timeline_text(self) -> str:
        return self._timeline_text.plain

    def list_text(self) -> str:
        return self._list_text
