"""Motion that carries meaning, at no cost when nothing moves.

One ticker (at most 15 fps) runs only while something animates: a working agent's
spinner, a needs-input pulse, or a short effect (flash, fade, slide, flight). When
nothing moves it stops, so an idle dashboard uses no CPU between refreshes.
Reduced motion (--no-anim, NO_COLOR, CCDASH_REDUCED_MOTION=1) disables all of it.
"""

from __future__ import annotations

import math
import time

from .theme import SPINNER

FPS = 15


def lerp(a: float, b: float, f: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, f))


def lerp_hex(a: str, b: str, f: float) -> str:
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(lerp(x, y, f)):02x}" for x, y in zip(ca, cb))


def spinner_glyph(now: float | None = None) -> str:
    """Braille spinner at ~10 fps, from the clock (smooth at any tick rate)."""
    return SPINNER[int((now or time.monotonic()) * 10) % len(SPINNER)]


PULSE_LOW, PULSE_HIGH = "#8a7428", "#ffd866"


def pulse_color(now: float | None = None, period: float = 1.6, steps: int = 6) -> str:
    """A gentle yellow pulse, quantized so unchanged frames redraw nothing."""
    phase = 0.5 + 0.5 * math.sin(2 * math.pi * (now or time.monotonic()) / period)
    return lerp_hex(PULSE_LOW, PULSE_HIGH, round(phase * steps) / steps)


class Animator:
    def __init__(self, app, enabled: bool):
        self.app = app
        self.enabled = enabled
        self.timer = None
        self.effects: dict = {}  # key -> (start, duration)

    @property
    def running(self) -> bool:
        return self.timer is not None

    def start(self, key, duration: float) -> None:
        if not self.enabled:
            return
        self.effects[key] = (time.monotonic(), duration)
        self.ensure()

    def progress(self, key) -> float | None:
        """0..1 while the effect runs, None when it is over (or never started)."""
        item = self.effects.get(key)
        if item is None:
            return None
        start, dur = item
        p = (time.monotonic() - start) / dur
        if p >= 1.0:
            del self.effects[key]
            return None
        return p

    def active(self, key) -> bool:
        return self.progress(key) is not None

    def any_effects(self) -> bool:
        for key in list(self.effects):
            self.progress(key)
        return bool(self.effects)

    def wanted(self) -> bool:
        return self.enabled and (self.any_effects() or self.app.anything_moving())

    def ensure(self) -> None:
        want = self.wanted()
        if want and self.timer is None:
            self.timer = self.app.set_interval(1 / FPS, self.tick, name="anim")
        elif not want and self.timer is not None:
            self.timer.stop()
            self.timer = None

    def tick(self) -> None:
        self.app.animate_frame()
        self.ensure()
