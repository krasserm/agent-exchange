from __future__ import annotations

from agent_session.agents._base import Agent
from agent_session.agents.claude import ClaudeAgent
from agent_session.agents.codex import CodexAgent

DEFAULT_AGENT = "claude"

_REGISTRY: dict[str, Agent] = {}


def register_agent(agent: Agent) -> None:
    """Register an agent implementation under its ``name``."""
    _REGISTRY[agent.name] = agent


def get_agent(name: str | None = None) -> Agent:
    """Return the registered agent for *name* (default: claude)."""
    key = name or DEFAULT_AGENT
    try:
        return _REGISTRY[key]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ValueError(f"unknown agent '{key}'; available: {known}")


def agent_names() -> list[str]:
    """Names of all registered agents, sorted."""
    return sorted(_REGISTRY)


register_agent(ClaudeAgent())
register_agent(CodexAgent())
