"""Render the first prompt of an agent: the coordination protocol, the live agent list and the task."""

from __future__ import annotations

from importlib import resources
from string import Template

from .model import AgentRecord

MAX_PROMPT = 30000  # Windows caps a command line at 32,767 characters


def protocol_template() -> str:
    return resources.files("ccdash").joinpath("protocol.md").read_text(encoding="utf-8")


def render_prompt(rec: AgentRecord, *, repo_name: str, default_branch: str, others: list[AgentRecord],
                  owned: dict[str, list[str]] | None = None, shared: list[str] | None = None) -> str:
    owned = owned or {}
    lines = []
    for o in others:
        claims = owned.get(o.name)
        scope = f" · works on {', '.join(claims[:6])}" if claims else ""
        lines.append(f"- **{o.name}** (`{o.branch}`): {o.task.splitlines()[0][:120]}{scope}")
    agents = "\n".join(lines) if lines else (
        "(none: you are the only agent right now. Check `ListAgents` before editing shared files, "
        "since others may start later.)")
    text = Template(protocol_template()).safe_substitute(
        name=rec.name,
        repo=repo_name,
        worktree=rec.worktree,
        branch=rec.branch,
        base_ref=rec.base_ref,
        base_sha=rec.base_sha[:7],
        default_branch=default_branch,
        agents=agents,
        shared=", ".join(f"`{s}`" for s in shared) if shared else "shared types, APIs, schemas, lockfiles",
        task=rec.task.strip(),
    )
    if len(text) > MAX_PROMPT:  # keep the task; shorten the agent list
        keep = MAX_PROMPT - (len(text) - len(agents)) - 40
        text = text.replace(agents, agents[:max(0, keep)] + "\n- … (more: use ListAgents)")
    return text
