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

Rect = tuple[int, int, int, int]  # left, top, width, height, in character cells


@dataclass
class Flight:
    sender: str
    to: str
    start: float

    def progress(self, now: float) -> float:
        return (now - self.start) / FLIGHT


def _edge_width(count: int) -> int:
    return 0 if count <= 0 else 1 if count <= 2 else 2 if count <= 5 else 3


# -- layout -------------------------------------------------------------------------
def _hit(a: Rect, b: Rect) -> bool:
    """Do two labels overlap? One blank column must stay between neighbours on a row."""
    return a[0] < b[0] + b[2] + 1 and b[0] < a[0] + a[2] + 1 and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


_NUDGES = sorted(((dx, dy) for dy in range(-3, 4) for dx in range(-18, 19, 2)),
                 key=lambda d: (abs(d[1]) * 5 + abs(d[0]), d))


def _free_spot(rect: Rect, placed: list[Rect], cols: int, rows: int) -> Rect:
    left, top, w, h = rect
    for dx, dy in _NUDGES:  # nearest first; a row costs as much as five columns
        cand = (left + dx, top + dy, w, h)
        if cand[0] < 0 or cand[1] < 0 or cand[0] + w > cols or cand[1] + h > rows:
            continue
        if not any(_hit(cand, r) for r in placed):
            return cand
    return rect  # no room at all (a tiny panel): overlap rather than vanish


def place_labels(sizes: dict[str, tuple[int, int]], cols: int, rows: int,
                 avoid: tuple[Rect, ...] = ()) -> dict[str, Rect]:
    """Where each node's label goes: on an ellipse sized so that every label fits inside
    the canvas, then moved to the nearest free spot so that no two labels overlap.

    `sizes` maps a node to its label's (width, height) in cells; `avoid` are cells that
    are already taken (the hub in the middle). The order of `sizes` is the order around
    the ellipse, so callers pass a stable order and nodes never jump."""
    if not sizes:
        return {}
    n = len(sizes)
    max_w = max(w for w, _ in sizes.values())
    max_h = max(h for _, h in sizes.values())
    cx, cy = cols / 2, (rows - max_h) / 2
    rx, ry = max(0.0, (cols - max_w) / 2 - 1), max(0.0, (rows - max_h) / 2)
    start = {2: math.pi, 4: -3 * math.pi / 4}.get(n, -math.pi / 2)  # two: left and right; four: the corners
    placed = list(avoid)
    out: dict[str, Rect] = {}
    for i, (name, (w, h)) in enumerate(sizes.items()):
        a = start + 2 * math.pi * i / n
        left = round(cx + rx * math.cos(a) - w / 2)
        top = round(cy + ry * math.sin(a))
        left = max(0, min(left, cols - w))
        top = max(0, min(top, rows - h))
        rect = _free_spot((left, top, w, h), placed, cols, rows)
        placed.append(rect)
        out[name] = rect
    return out


def _size(lines: list[Text]) -> tuple[int, int]:
    return max(len(t.plain) for t in lines), len(lines)


def _centred(lines: list[Text], cols: int, rows: int) -> Rect:
    w, h = _size(lines)
    return (max(0, (cols - w) // 2), max(0, (rows - h) // 2), w, h)


def _anchor(rect: Rect, lines: list[Text]) -> tuple[float, float]:
    """Where edges meet a label, in dot coordinates: the middle of its first line."""
    return (rect[0] * 2 + len(lines[0].plain), rect[1] * 4 + 2)


def _chars(text: Text):
    styles = [""] * len(text.plain)
    for span in text.spans:
        for i in range(span.start, min(span.end, len(styles))):
            styles[i] = str(span.style)
    base = str(text.style) if text.style else ""
    return [(ch, styles[i] or base) for i, ch in enumerate(text.plain)]


def _put(canvas: BrailleCanvas, rect: Rect, lines: list[Text]) -> None:
    """Draw a label over the edges, with a blank cell on each side so lines do not touch the text."""
    left, top = rect[0], rect[1]
    for r, text in enumerate(lines):
        canvas.text(left - 1, top + r, " ")
        for i, (ch, style) in enumerate(_chars(text)):
            canvas.text(left + i, top + r, ch, style)
        canvas.text(left + len(text.plain), top + r, " ")


def _dot(canvas: BrailleCanvas, a: tuple[float, float], b: tuple[float, float], p: float, glyph: str,
         style: str) -> None:
    canvas.text(int(lerp(a[0], b[0], p) // 2), int(lerp(a[1], b[1], p) // 4), glyph, style)


def branch_bar(ahead: int, behind: int) -> Text:
    blocks = min(6, ahead)
    t = Text()
    t.append(f"+{ahead} ", style=ACCENT if ahead else DIM)
    t.append(f"−{behind}", style="yellow" if behind > 20 else DIM)
    if blocks:
        t.append(" " + "▮" * blocks, style=ACCENT)
    return t


class Surface(Static):
    """A Static whose drawing depends on its real size: it is painted again whenever it is
    laid out or resized (a view that was hidden has no size until then)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.painter = None  # (cols, rows) -> None
        self.estimate = None  # () -> (cols, rows), used before the first layout
        self._painted: tuple[int, int] | None = None

    def repaint(self) -> None:
        if self.painter is None:
            return
        w, h = self.size.width, self.size.height
        if w < 8:
            if self.estimate is None:
                return  # on_resize paints once the size is known
            w, h = self.estimate()
        else:
            self._painted = (w, h)
        self.painter(w, h)

    def on_resize(self, event) -> None:
        if (self.size.width, self.size.height) != self._painted:
            self.repaint()


class NetworkView(Surface):
    """Agents around the default branch; edges are messages (thickness = count in the last
    15 min), red edges are overlaps or predicted conflicts, each node has a tiny branch bar."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.flights: list[Flight] = []
        self._edges: dict[frozenset, tuple[str, int]] = {}
        self._scene = None
        self.last_text = Text()
        self.painter = self._paint

    def edge_style(self, a: str, b: str) -> str | None:
        e = self._edges.get(frozenset({a, b}))
        return e[0] if e else None

    def edge_width(self, a: str, b: str) -> int:
        e = self._edges.get(frozenset({a, b}))
        return e[1] if e else 0

    def add_flight(self, sender: str, to: str) -> None:
        self.flights.append(Flight(sender, to, time.monotonic()))

    def show(self, views, messages, red_pairs: set, *, animate: bool, merged_progress=None,
             default_branch: str = "main") -> None:
        now = time.time()
        agents = sorted((v for v in views if v.status not in ("untracked", "deleted")),
                        key=lambda v: (v.created_at or "~", v.name))  # a stable order: nodes never jump
        names = {v.name for v in agents}
        counts: dict[frozenset, int] = {}
        recent: set[frozenset] = set()
        for m in messages:
            if now - m.t > WINDOW:
                continue
            for to in (m.recipients if m.to in ("all", "*") else [m.to]):
                if m.sender in names and to in names and to != m.sender:
                    key = frozenset({m.sender, to})
                    counts[key] = counts.get(key, 0) + 1
                    if now - m.t < 120:
                        recent.add(key)
        edges: dict[frozenset, tuple[str, int]] = {}
        for key, n in counts.items():
            edges[key] = ("red" if key in red_pairs else (ACCENT if key in recent else "default"), _edge_width(n))
        for key in red_pairs:
            if key not in edges and len(key) == 2 and all(k in names or k == default_branch for k in key):
                edges[key] = ("red", 1)
        self._edges = edges
        self._scene = (agents, animate, merged_progress, default_branch)
        self.repaint()

    def _paint(self, cols: int, rows: int) -> None:
        if self._scene is None or rows < 3:
            return
        agents, animate, merged_progress, default = self._scene
        now_m = time.monotonic()
        self.flights = [f for f in self.flights if f.progress(now_m) < 1.0]
        canvas = BrailleCanvas(cols, rows)
        labels: dict[str, list[Text]] = {}
        for v in agents:
            glyph = spinner_glyph() if animate and v.status == "working" else STATUS_GLYPH.get(v.status, " ")
            labels[v.name] = [Text.assemble((glyph + " ", STATUS_STYLE.get(v.status, "default")), (v.name, "bold")),
                              branch_bar(v.ahead, v.behind)]
        hub = [Text(f"◆ {default}", style=f"bold {ACCENT}")]
        hub_rect = _centred(hub, cols, rows)
        rects = place_labels({n: _size(lines) for n, lines in labels.items()}, cols, rows, avoid=(hub_rect,))
        for v in agents:
            p = merged_progress(v.name) if merged_progress else None
            if p is not None:  # a merged branch flows into the default branch
                left, top, w, h = rects[v.name]
                rects[v.name] = (round(lerp(left, hub_rect[0], p)), round(lerp(top, hub_rect[1], p)), w, h)
        main = _anchor(hub_rect, hub)
        pos = {n: _anchor(rects[n], labels[n]) for n in labels}
        for v in agents:  # every branch hangs off the default branch
            if frozenset({v.name, default}) in self._edges:
                canvas.line(*pos[v.name], *main, style="red", rank=2)
            else:
                canvas.line(*pos[v.name], *main, style="blue" if v.merge_state == "merged" else DIM, dotted=True,
                            rank=0)
        for key, (style, width) in self._edges.items():
            if all(k in pos for k in key):
                a, b = sorted(key)
                canvas.line(*pos[a], *pos[b], style=style, width=max(1, width), rank=2 if style == "red" else 1)
        for f in self.flights:
            if f.sender in pos and f.to in pos:
                _dot(canvas, pos[f.sender], pos[f.to], f.progress(now_m), "●", f"bold {ACCENT}")
        _put(canvas, hub_rect, hub)
        for name, lines in labels.items():
            _put(canvas, rects[name], lines)
        self.last_text = canvas.render()
        self.update(self.last_text)


class CommsView(Vertical):
    """Full-screen agent-to-agent communication: graph, conversations, transcript, timeline."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.threads: list = []
        self._scene = None
        self._thread_sig = None
        self._timeline_text = Text()
        self._graph_text = Text()
        self._list_text = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="comms-top"):
            yield Surface(id="comms-graph", classes="panel")
            with Vertical(id="comms-right"):
                yield OptionList(id="comms-threads", classes="panel")
                yield Static(id="comms-transcript", classes="panel")
        yield Surface(id="comms-timeline", classes="panel")

    def on_mount(self) -> None:
        graph, timeline = self.query_one("#comms-graph", Surface), self.query_one("#comms-timeline", Surface)
        graph.border_title = "Graph"
        graph.painter = self._paint_graph
        timeline.border_title = "Timeline · last 15 min"
        timeline.painter = self._paint_timeline
        timeline.estimate = lambda: (max(40, self.app.size.width - 4), 0)  # its height follows its content
        self.query_one("#comms-threads").border_title = "Conversations"
        self.query_one("#comms-transcript").border_title = "Transcript"

    @property
    def selected(self):
        ol = self.query_one("#comms-threads", OptionList)
        if ol.highlighted is None or not self.threads or ol.highlighted >= len(self.threads):
            return None
        return self.threads[ol.highlighted]

    def show(self, views, messages, *, animate: bool, flights: list) -> None:
        now = time.time()
        self.threads = comms.build_threads(messages, now=now)
        self._scene = (list(views), list(messages), animate, flights, now)
        self._render_list(animate)
        self._repaint()

    def _repaint(self) -> None:
        if self._scene is None:
            return
        self._render_transcript(self.selected)
        self.query_one("#comms-graph", Surface).repaint()
        self.query_one("#comms-timeline", Surface).repaint()

    def on_option_list_option_highlighted(self, event) -> None:
        """Selecting a conversation highlights its edge and its arrows, and shows its transcript."""
        self._repaint()

    def on_resize(self, event) -> None:
        if self._scene is not None:
            self._render_list(self._scene[2])

    def _render_list(self, animate: bool) -> None:
        ol = self.query_one("#comms-threads", OptionList)
        width = (ol.size.width or max(30, self.app.size.width // 2 - 4)) - 3  # option padding, scrollbar
        lines = []
        for t in self.threads:
            state = t.state
            glyph = spinner_glyph() if (animate and state == "discussing") else {
                "agreed": "✓", "acked": "✓✓", "open": "○", "delivered": "→", "discussing": "●"}.get(state, " ")
            clock = theme.fmt_clock(t.last_t)
            room = width - 14 - len(t.pair_label) - 2 - len(clock) - 2  # one line per conversation
            topic = t.topic if len(t.topic) <= room else (t.topic[:room - 1] + "…" if room > 1 else "")
            row = Text(no_wrap=True, overflow="ellipsis")
            row.append(f"{glyph:<2} ", style=THREAD_STYLE.get(state, "default"))
            row.append(f"{state:<10} ", style=THREAD_STYLE.get(state, "default"))
            row.append(t.pair_label, style="bold")
            row.append(f"  {topic}", style=DIM)
            row.append("  " + clock, style=DIM)
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

    def _paint_graph(self, cols: int, rows: int) -> None:
        if self._scene is None or rows < 3:
            return
        views, messages, animate, flights, _now = self._scene
        sel = self.selected
        canvas = BrailleCanvas(cols, rows)
        names = sorted(v.name for v in views if v.status not in ("untracked", "deleted"))
        for m in messages:
            for n in [m.sender, *(m.recipients if m.to in ("all", "*") else [m.to])]:
                if n not in names and n not in ("all", "*", "you", "ccdash"):
                    names.append(n)
        names += ["you", "ccdash"]
        labels = {n: [Text(n, style=ACCENT if n == "you" else (DIM if n == "ccdash" else "bold"))] for n in names}
        rects = place_labels({n: _size(lines) for n, lines in labels.items()}, cols, rows)
        pos = {n: _anchor(rects[n], labels[n]) for n in names}
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
                _dot(canvas, pos[a], pos[b], (time.monotonic() / 1.5) % 1.0, "•", ACCENT)
        for f in flights:
            if f.sender in pos and f.to in pos:
                _dot(canvas, pos[f.sender], pos[f.to], f.progress(time.monotonic()), "●", f"bold {ACCENT}")
        for n in names:
            _put(canvas, rects[n], labels[n])
        self._graph_text = canvas.render()
        self.query_one("#comms-graph", Surface).update(self._graph_text)

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

    def _paint_timeline(self, cols: int, rows: int) -> None:
        if self._scene is None:
            return
        views, messages, animate, _flights, now = self._scene
        sel = self.selected
        lanes = [v.name for v in views if v.status not in ("untracked", "deleted")]
        recent = [m for m in messages if now - m.t <= WINDOW]
        for m in recent:
            for n in [m.sender, *(m.recipients if m.to in ("all", "*") else [m.to])]:
                if n not in lanes and n not in ("all", "*"):
                    lanes.append(n)
        label_w = max([len(x) for x in lanes] + [6]) + 1
        width = max(20, cols - label_w - 3)
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
        self.query_one("#comms-timeline", Surface).update(out)

    def timeline_text(self) -> str:
        return self._timeline_text.plain

    def graph_text(self) -> str:
        return self._graph_text.plain

    def list_text(self) -> str:
        return self._list_text
