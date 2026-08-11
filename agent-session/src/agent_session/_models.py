from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel


class AgentStatus(StrEnum):
    IDLE = "idle"
    WORKING = "working"
    WAITING = "waiting"
    # The turn ended but background work (background shells, subagents,
    # workflows) is still in flight -- not the same as a genuinely idle agent.
    BACKGROUNDED = "backgrounded"
    # The agent is alive on its remote/container host but the local tmux proxy
    # cannot currently reach it (network partition, or the local tmux was killed
    # directly). Distinct from "ended" -- the session is recoverable by
    # reattaching, and a real SessionEnd would set it to ended instead.
    DISCONNECTED = "disconnected"


class EventStatus(StrEnum):
    ENDED = "ended"
    IDLE = "idle"
    WAITING_FOR_INPUT = "waiting_for_input"
    WORKING = "working"
    BACKGROUNDED = "backgrounded"


class Event(BaseModel):
    timestamp: datetime
    event: str
    status: EventStatus
    session_id: str
    agent_id: str = "n/a"
    agent_type: str = "n/a"
    assistant_message: str | None = None
    user_message: str | None = None
    transcript_path: Path | None = None
    # Counts of in-flight background work / scheduled wakeups at event time.
    # Only Stop/SubagentStop payloads from recent Claude Code versions carry
    # the source fields; everything else defaults to 0.
    background_tasks: int = 0
    session_crons: int = 0


class AssistantMessage(BaseModel):
    message: str
    timestamp: datetime


class AgentInfo(BaseModel):
    # session_id may be None for agents (e.g. codex) that only assign one once
    # the first turn begins, while the session is otherwise ready and idle.
    session_id: str | None = None
    status: AgentStatus
    transcript_path: Path | None = None
    background_tasks: int = 0
    session_crons: int = 0


def event_status_to_agent_status(status: EventStatus) -> AgentStatus | None:
    match status:
        case EventStatus.IDLE:
            return AgentStatus.IDLE
        case EventStatus.WORKING:
            return AgentStatus.WORKING
        case EventStatus.WAITING_FOR_INPUT:
            return AgentStatus.WAITING
        case EventStatus.BACKGROUNDED:
            return AgentStatus.BACKGROUNDED
        case EventStatus.ENDED:
            return None
