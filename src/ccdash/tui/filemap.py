"""File map (f): who is in which part of the code. The tree of touched files, each in the
color of the agent that touched it; a file touched by two or more agents is red and striped."""

from __future__ import annotations

from rich.text import Text

from .network import Surface
from .theme import DIM

# One color per agent. No red in here: red means two agents are in the same file.
PALETTE = ("#5fafff", "#87d787", "#d7af5f", "#d787d7", "#5fd7d7", "#afaf5f", "#af87ff", "#ff8fb0")


def agent_colors(names) -> dict[str, str]:
    return {n: PALETTE[i % len(PALETTE)] for i, n in enumerate(sorted(names))}


def tree_lines(touch: dict[str, list[str]]) -> list[tuple[str, str, list[str] | None]]:
    """(tree prefix, name, agents) per line, directories first; agents is None for a directory.
    A chain of directories with nothing else in them shares one line (`src/ui/`)."""
    root: dict = {}
    for path, agents in touch.items():
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        if not parts:
            continue
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part + "/", {})
        node[parts[-1]] = list(agents)
    out: list[tuple[str, str, list[str] | None]] = []

    def walk(node: dict, prefix: str) -> None:
        items = sorted(node.items(), key=lambda kv: (not isinstance(kv[1], dict), kv[0].lower()))
        for i, (name, child) in enumerate(items):
            last = i == len(items) - 1
            if isinstance(child, dict):
                while len(child) == 1 and isinstance(next(iter(child.values())), dict):
                    sub, child = next(iter(child.items()))
                    name += sub
                out.append((prefix + ("└─ " if last else "├─ "), name, None))
                walk(child, prefix + ("   " if last else "│  "))
            else:
                out.append((prefix + ("└─ " if last else "├─ "), name, child))

    walk(root, "")
    return out


def striped(name: str) -> Text:
    t = Text()
    for i, ch in enumerate(name):
        t.append(ch, style="bold red" if (i // 2) % 2 == 0 else "bold red reverse")
    return t


class FileMapView(Surface):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._scene: tuple[dict, str | None] | None = None
        self.last_text = Text()
        self.painter = self._paint

    def show(self, touch: dict[str, list[str]], selected: str | None = None) -> None:
        self._scene = (dict(touch), selected)
        self.repaint()

    def _paint(self, cols: int, rows: int) -> None:
        if self._scene is None:
            return
        touch, selected = self._scene
        t = Text(no_wrap=True, overflow="ellipsis")
        if not touch:
            t.append("No agent has touched a file yet.", style=DIM)
            self.last_text = t
            self.update(t)
            return
        colors = agent_colors({a for agents in touch.values() for a in agents})
        both = sum(len(agents) > 1 for agents in touch.values())
        t.append(f"{len(touch)} file{'s' if len(touch) != 1 else ''} touched by {len(colors)} "
                 f"agent{'s' if len(colors) != 1 else ''}", style=DIM)
        if both:
            t.append(" · ", style=DIM)
            t.append_text(striped("▚▚▚▚"))
            t.append(f" {both} by more than one", style="red")
        t.append("\n")
        lines = tree_lines(touch)
        room = max(2, rows - 1)
        shown = lines if len(lines) <= room else lines[:room - 1]
        column = min(max(len(p) + len(n) for p, n, _ in shown) + 2, max(20, cols - 24))
        for prefix, name, agents in shown:
            t.append(prefix, style=DIM)
            if agents is None:
                t.append(name, style=DIM)
            elif len(agents) > 1:
                t.append_text(striped(name))
                t.append(" " * max(2, column - len(prefix) - len(name)))
                for i, agent in enumerate(agents):
                    if i:
                        t.append(" × ", style="red")
                    t.append(agent, style=colors.get(agent, "default"))
            else:
                agent = agents[0]
                style = f"bold {colors[agent]}" if agent == selected else colors[agent]
                t.append(name, style=style)
                t.append(" " * max(2, column - len(prefix) - len(name)))
                t.append(agent, style=style)
            t.append("\n")
        if len(lines) > len(shown):
            t.append(f"… {len(lines) - len(shown)} more", style=DIM)
        else:
            t.rstrip()
        self.last_text = t
        self.update(t)
