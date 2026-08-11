"""Runtime configuration for agent-session-web (env-driven, localhost-only by default)."""

from __future__ import annotations

import os
from pathlib import Path


def cs_binary() -> str:
    """Path/name of the `asn` CLI used for all session operations."""
    return os.environ.get("AGENT_SESSION_WEB_CS_BINARY", "asn")


def cs_timeout() -> float:
    """Default timeout (seconds) for `asn` subprocess calls."""
    return float(os.environ.get("AGENT_SESSION_WEB_CS_TIMEOUT", "10"))


def server_host() -> str:
    """Bind host (localhost-only by default)."""
    return os.environ.get("AGENT_SESSION_WEB_HOST", "127.0.0.1")


def server_port() -> int:
    """Bind port."""
    return int(os.environ.get("AGENT_SESSION_WEB_PORT", "8770"))


def dist_dir() -> Path:
    """Directory holding the built frontend (Vite `frontend/dist`)."""
    override = os.environ.get("AGENT_SESSION_WEB_DIST_DIR")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
