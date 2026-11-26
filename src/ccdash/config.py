"""`.ccdash.toml` in the repo, overridden by the user config file."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

PERMISSION_MODES = ("default", "manual", "acceptEdits", "auto", "dontAsk", "plan", "bypassPermissions")


class ConfigError(Exception):
    pass


@dataclass
class Config:
    default_branch: str | None = None  # auto-detected from origin/HEAD if omitted
    branch_prefix: str = "cc/"
    worktree_dir: str = ".claude/worktrees"
    setup: str | None = None  # run once in each new worktree
    test: str | None = None  # used by local merge
    permission_mode: str | None = None  # None: agents use the user's own defaultMode
    model: str | None = None
    guard: str = "off"  # off | warn | block
    shared: list[str] = field(default_factory=list)
    max_agents: int = 6
    runner: str = "auto"  # auto | winpty | tmux | bg
    hook_transport: str = "auto"  # auto | http | command
    animations: bool = True
    stale_base_commits: int = 20
    shared_notice_after: int = 120  # seconds before ccdash broadcasts an unannounced shared change
    repos: list[str] = field(default_factory=list)  # extra repos shown in the same screen
    open_in: str = "none"  # none | wt | vscode   (open attach in a new terminal tab)
    claude_command: list[str] | None = None  # override the claude executable (tests use a fake)
    sources: list[str] = field(default_factory=list, repr=False)


_CHOICES = {
    "guard": ("off", "warn", "block"),
    "runner": ("auto", "winpty", "tmux", "bg"),
    "hook_transport": ("auto", "http", "command"),
    "open_in": ("none", "wt", "vscode"),
    "permission_mode": PERMISSION_MODES,
}


def user_config_path() -> Path:
    if os.environ.get("CCDASH_CONFIG"):
        return Path(os.environ["CCDASH_CONFIG"])
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "ccdash" / "config.toml"


def _read(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def _apply(cfg: Config, data: dict, source: Path) -> None:
    types = {f.name: f for f in fields(Config)}
    for key, value in data.items():
        if key not in types or key == "sources":
            raise ConfigError(f"{source}: unknown key {key!r}")
        default = getattr(Config(), key)
        expected = type(default) if default is not None else None
        if key in ("default_branch", "setup", "test", "permission_mode", "model"):
            ok = isinstance(value, str)
        elif key == "claude_command":
            ok = isinstance(value, list) and all(isinstance(x, str) for x in value)
        elif expected is list:
            ok = isinstance(value, list) and all(isinstance(x, str) for x in value)
        elif expected is bool:
            ok = isinstance(value, bool)
        elif expected is int:
            ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else:
            ok = isinstance(value, str)
        if not ok:
            raise ConfigError(f"{source}: {key} has the wrong type ({value!r})")
        if key in _CHOICES and value not in _CHOICES[key]:
            raise ConfigError(f"{source}: {key} must be one of {', '.join(_CHOICES[key])} (got {value!r})")
        setattr(cfg, key, value)
    cfg.sources.append(str(source))


def load_config(main_root: Path) -> Config:
    cfg = Config()
    for path in (Path(main_root) / ".ccdash.toml", user_config_path()):
        data = _read(path)
        if data:
            _apply(cfg, data, path)
    if cfg.permission_mode == "manual":
        cfg.permission_mode = "default"
    if not cfg.branch_prefix or cfg.branch_prefix.startswith("-"):
        raise ConfigError("branch_prefix must not be empty or start with '-'")
    return cfg
