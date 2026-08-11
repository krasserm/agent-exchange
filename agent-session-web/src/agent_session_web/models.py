"""Pydantic models forming the REST/WS contract shared with the typed frontend."""

from __future__ import annotations

from pydantic import BaseModel


class SessionModel(BaseModel):
    """A session as returned by `asn list --json` plus a derived display label."""

    session_key: str
    tmux_session_name: str | None
    claude_start_dir: str
    host: str | None = None  # ssh host for remote sessions; None when local
    status: str | None
    session_id: str | None
    project: str
    background_tasks: int = 0
    session_crons: int = 0


class SessionDetailModel(SessionModel):
    """Richer schema from `asn status KEY --json`."""

    claude_project_dir: str | None
    transcript_path: str | None


class MessageModel(BaseModel):
    """A recent assistant message from `asn messages KEY --json`."""

    message: str
    timestamp: str


class SendRequest(BaseModel):
    """Body for POST .../messages."""

    message: str


class SendResponse(BaseModel):
    """Result of sending a message to a session."""

    ok: bool


class StopResponse(BaseModel):
    """Result of stopping a session."""

    ok: bool


class ResizeMessage(BaseModel):
    """Terminal resize control frame (text WS frame)."""

    type: str
    cols: int
    rows: int
