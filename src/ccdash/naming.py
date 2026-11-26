"""Agent names: slugs derived from the task, unique within a repo."""

import re
import unicodedata

MAX_LEN = 30
_WORD = re.compile(r"[a-z0-9]+")
_VALID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?")
# Device names Windows refuses as file or directory names.
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def _ascii(text: str) -> str:
    text = text.replace("ß", "ss").replace("ẞ", "SS")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def slugify(task: str, words: int = 4, max_len: int = MAX_LEN) -> str:
    """Lowercase, first `words` words, at most `max_len` chars; 'agent' if nothing is left."""
    parts = _WORD.findall(_ascii(task).lower())[:words]
    slug = "-".join(parts)[:max_len].strip("-")
    if not slug or slug in _RESERVED:
        slug = f"{slug}-agent" if slug else "agent"
    return slug


def unique_name(base: str, taken: set[str], max_len: int = MAX_LEN) -> str:
    """`base`, or `base-2`, `base-3`, ... trimmed so the result still fits `max_len`."""
    if base not in taken:
        return base
    i = 2
    while True:
        suffix = f"-{i}"
        cand = base[: max_len - len(suffix)].rstrip("-") + suffix
        if cand not in taken:
            return cand
        i += 1


def is_valid_name(name: str) -> bool:
    return bool(_VALID.fullmatch(name or "")) and name not in _RESERVED
