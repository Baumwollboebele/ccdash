"""Render the first prompt of an agent: the coordination protocol, the live agent list and the task."""

from __future__ import annotations

from importlib import resources
from string import Template

from .model import AgentRecord

MAX_PROMPT = 30000  # Windows caps a command line at 32,767 characters


def protocol_template() -> str:
    return resources.files("ccdash").joinpath("protocol.md").read_text(encoding="utf-8")


LEAD_LINE = ("- **lead** (the user's coordinator; it has no branch and edits nothing): when you are done or blocked, "
             "send it one line with `SendMessage`.")


def _agent_lines(others: list[AgentRecord], owned: dict[str, list[str]] | None = None) -> list[str]:
    owned = owned or {}
    lines = []
    for o in others:
        claims = owned.get(o.name)
        scope = f" · works on {', '.join(claims[:6])}" if claims else ""
        lines.append(f"- **{o.name}** (`{o.branch}`): {o.task.splitlines()[0][:120]}{scope}")
    return lines


def render_lead_prompt(*, repo_name: str, default_branch: str, cmd: str, prefix: str, max_agents: int,
                       others: list[AgentRecord], request: str | None = None,
                       owned: dict[str, list[str]] | None = None) -> str:
    """The first prompt of the lead (lead.md): how to delegate through ccdash, and the user's request."""
    from .lead import NO_REQUEST

    template = resources.files("ccdash").joinpath("lead.md").read_text(encoding="utf-8")
    agents = "\n".join(_agent_lines(others, owned)) or "(none yet)"
    return Template(template).safe_substitute(
        repo=repo_name, default_branch=default_branch, cmd=cmd, prefix=prefix, max_agents=max_agents,
        agents=agents[:8000], request=(request or "").strip() or NO_REQUEST)


def render_prompt(rec: AgentRecord, *, repo_name: str, default_branch: str, others: list[AgentRecord],
                  owned: dict[str, list[str]] | None = None, shared: list[str] | None = None,
                  lead: bool = False) -> str:
    lines = _agent_lines(others, owned) or [
        "(no other agents right now. Check `ListAgents` before editing shared files, since others may start later.)"]
    if lead:
        lines.insert(0, LEAD_LINE)
    agents = "\n".join(lines)
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
