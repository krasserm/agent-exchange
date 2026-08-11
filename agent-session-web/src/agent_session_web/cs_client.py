"""Async wrapper around the `asn` CLI — the ONLY source of session data.

Every call shells out to `asn` via ``asyncio.create_subprocess_exec`` and never
blocks the event loop. We read ``tmux_session_name`` straight from `asn` output;
we never talk to the daemon socket and never re-derive tmux names.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from . import config
from .models import MessageModel, SessionDetailModel, SessionModel


class CsError(RuntimeError):
    """A `asn` invocation failed (non-zero exit, timeout, or bad output)."""


async def _run_cs(*args: str, timeout: float | None = None) -> str:
    """Run `asn <args>` and return stdout, raising CsError on failure/timeout."""
    binary = config.cs_binary()
    effective_timeout = config.cs_timeout() if timeout is None else timeout

    proc = await asyncio.create_subprocess_exec(
        binary,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=effective_timeout
        )
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()  # reap to avoid a zombie
        raise CsError(f"asn {' '.join(args)} timed out after {effective_timeout}s") from exc

    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip() or f"exit {proc.returncode}"
        raise CsError(f"asn {' '.join(args)} failed: {detail}")

    return stdout.decode(errors="replace")


async def _run_cs_json(*args: str, timeout: float | None = None) -> Any:
    """Run `asn <args>` expecting JSON stdout; raise CsError on parse failure."""
    out = await _run_cs(*args, timeout=timeout)
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise CsError(f"asn {' '.join(args)} returned non-JSON output") from exc


def _project_label(claude_start_dir: str) -> str:
    """Display label for a session: basename of its start dir."""
    name = Path(claude_start_dir).name
    return name or claude_start_dir


def _start_dir(raw: dict[str, Any]) -> str:
    """Session start dir. agent-session emits ``start_dir``; legacy agent-session
    emitted ``claude_start_dir`` — accept either."""
    return raw.get("start_dir") or raw.get("claude_start_dir") or ""


def _project_dir(raw: dict[str, Any]) -> str | None:
    """Claude project dir. agent-session emits ``project_dir``; legacy
    agent-session emitted ``claude_project_dir`` — accept either."""
    return raw.get("project_dir") or raw.get("claude_project_dir")


def _to_session(raw: dict[str, Any]) -> SessionModel:
    start_dir = _start_dir(raw)
    return SessionModel(
        session_key=raw["session_key"],
        tmux_session_name=raw.get("tmux_session_name"),
        claude_start_dir=start_dir,
        host=raw.get("host"),
        status=raw.get("status"),
        session_id=raw.get("session_id"),
        project=_project_label(start_dir),
        background_tasks=raw.get("background_tasks") or 0,
        session_crons=raw.get("session_crons") or 0,
    )


def _to_detail(raw: dict[str, Any]) -> SessionDetailModel:
    start_dir = _start_dir(raw)
    return SessionDetailModel(
        session_key=raw["session_key"],
        tmux_session_name=raw.get("tmux_session_name"),
        claude_start_dir=start_dir,
        host=raw.get("host"),
        status=raw.get("status"),
        session_id=raw.get("session_id"),
        project=_project_label(start_dir),
        background_tasks=raw.get("background_tasks") or 0,
        session_crons=raw.get("session_crons") or 0,
        claude_project_dir=_project_dir(raw),
        transcript_path=raw.get("transcript_path"),
    )


async def list_sessions() -> list[SessionModel]:
    """Return all sessions from `asn list --json`; [] on any CsError."""
    try:
        raw = await _run_cs_json("list", "--json")
    except CsError:
        return []
    match raw:
        case list():
            return [_to_session(item) for item in raw]
        case _:
            return []


async def get_session(key: str, timeout: float | None = None) -> SessionDetailModel:
    """Return detailed status from `asn status KEY --json`."""
    raw = await _run_cs_json("status", key, "--json", timeout=timeout)
    return _to_detail(raw)


async def stop_session(key: str) -> None:
    """Stop a session via `asn stop KEY`."""
    await _run_cs("stop", key)


async def send_message(key: str, message: str) -> None:
    """Send a message via `asn send KEY message`."""
    await _run_cs("send", key, message)


async def get_messages(key: str, last: int = 5) -> list[MessageModel]:
    """Return recent assistant messages from `asn messages KEY --last N --json`."""
    raw = await _run_cs_json("messages", key, "--last", str(last), "--json")
    match raw:
        case list():
            return [MessageModel(message=m["message"], timestamp=m["timestamp"]) for m in raw]
        case _:
            return []


def collapse_home(path: str) -> str:
    """Collapse a leading $HOME to ``~`` for display."""
    home = os.path.expanduser("~")
    if path.startswith(home):
        return "~" + path[len(home):]
    return path
