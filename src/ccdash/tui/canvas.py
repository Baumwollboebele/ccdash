"""A braille canvas: 2x4 dots per character cell, for smooth lines in a terminal."""

from __future__ import annotations

from rich.text import Text

_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))


class BrailleCanvas:
    def __init__(self, cols: int, rows: int):
        self.cols, self.rows = max(1, cols), max(1, rows)
        self.dots = [0] * (self.cols * self.rows)
        self.style: list[tuple[int, str] | None] = [None] * (self.cols * self.rows)
        self.overlay: dict[tuple[int, int], tuple[str, str]] = {}

    @property
    def width(self) -> int:  # in dots
        return self.cols * 2

    @property
    def height(self) -> int:
        return self.rows * 4

    def set(self, x: int, y: int, style: str = "", rank: int = 0) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        i = (y // 4) * self.cols + x // 2
        self.dots[i] |= _BITS[y % 4][x % 2]
        cur = self.style[i]
        if cur is None or rank >= cur[0]:
            self.style[i] = (rank, style)

    def line(self, x0: float, y0: float, x1: float, y1: float, style: str = "", width: int = 1, dotted: bool = False,
             rank: int = 0) -> None:
        steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        dx, dy = x1 - x0, y1 - y0
        norm = max(1e-9, (dx * dx + dy * dy) ** 0.5)
        ox, oy = -dy / norm, dx / norm  # perpendicular, for thicker lines
        for s in range(steps + 1):
            if dotted and s % 3:
                continue
            f = s / max(1, steps)
            x, y = x0 + dx * f, y0 + dy * f
            for k in range(width):
                off = k - (width - 1) / 2
                self.set(round(x + ox * off), round(y + oy * off), style, rank)

    def text(self, col: int, row: int, s: str, style: str = "") -> None:
        for i, ch in enumerate(s):
            if 0 <= col + i < self.cols and 0 <= row < self.rows:
                self.overlay[(col + i, row)] = (ch, style)

    def render(self) -> Text:
        out = Text(no_wrap=True, overflow="crop")
        for r in range(self.rows):
            for c in range(self.cols):
                ov = self.overlay.get((c, r))
                if ov is not None:
                    out.append(ov[0], style=ov[1])
                    continue
                i = r * self.cols + c
                bits = self.dots[i]
                if bits:
                    st = self.style[i]
                    out.append(chr(0x2800 + bits), style=st[1] if st else "")
                else:
                    out.append(" ")
            if r < self.rows - 1:
                out.append("\n")
        return out
