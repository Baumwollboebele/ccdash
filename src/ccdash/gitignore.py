"""A small matcher for .gitignore-syntax pattern files (used for .worktreeinclude)."""

from __future__ import annotations

import re


def _translate(pat: str) -> str:
    """Translate one gitignore glob (without leading '!' / trailing '/') to a regex body."""
    i, n, out = 0, len(pat), []
    while i < n:
        c = pat[i]
        if c == "*":
            if pat[i:i + 2] == "**":
                before_ok = i == 0 or pat[i - 1] == "/"
                after = pat[i + 2:i + 3]
                if before_ok and after == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                if before_ok and after == "":
                    out.append(".*")
                    i += 2
                    continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pat.find("]", i + 1)
            if j < 0:
                out.append(re.escape(c))
            else:
                body = pat[i + 1:j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j
        elif c == "\\" and i + 1 < n:
            i += 1
            out.append(re.escape(pat[i]))
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


class PatternSet:
    def __init__(self, lines: list[str]):
        self.rules: list[tuple[re.Pattern, bool, bool]] = []  # (regex, negated, dir_only)
        self.raw: list[str] = []
        for line in lines:
            line = line.rstrip("\r\n")
            if not line.strip() or line.startswith("#"):
                continue
            line = line.rstrip(" ") if not line.endswith("\\ ") else line
            neg = line.startswith("!")
            if neg:
                line = line[1:]
            dir_only = line.endswith("/")
            line = line.rstrip("/")
            if not line:
                continue
            anchored = "/" in line
            line = line.lstrip("/")
            body = _translate(line)
            regex = f"^{body}$" if anchored else f"^(?:.*/)?{body}$"
            self.rules.append((re.compile(regex), neg, dir_only))
            self.raw.append(("!" if neg else "") + line + ("/" if dir_only else ""))

    @classmethod
    def from_file(cls, path) -> "PatternSet":
        try:
            with open(path, encoding="utf-8") as f:
                return cls(f.read().splitlines())
        except FileNotFoundError:
            return cls([])

    def __bool__(self) -> bool:
        return bool(self.rules)

    def _match_one(self, path: str, is_dir: bool) -> bool | None:
        result = None
        for regex, neg, dir_only in self.rules:
            if dir_only and not is_dir:
                continue
            if regex.match(path):
                result = not neg
        return result

    def matches(self, path: str) -> bool:
        """True if the file `path` (repo-relative, '/'-separated) or one of its parent dirs matches."""
        parts = path.strip("/").split("/")
        for k in range(1, len(parts)):
            if self._match_one("/".join(parts[:k]), True):
                return True
        return bool(self._match_one(path, False))

    def may_reach_into(self, dirpath: str) -> bool:
        """Could a pattern match something inside the (wholly ignored) directory `dirpath`?

        Like Claude Code: only patterns that start with `**/` or explicitly name a path
        inside the directory reach into it; plain basenames such as `.env` do not.
        """
        d = dirpath.strip("/") + "/"
        for raw in self.raw:
            p = raw.lstrip("!")
            if p.startswith("**/"):
                return True
            if "/" in p.rstrip("/") and (p.startswith(d) or d.startswith(p.rstrip("/") + "/")):
                return True
            if self._match_one(d.rstrip("/"), True):
                return True
        return False
