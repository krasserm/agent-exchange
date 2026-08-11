from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncGenerator
from pathlib import Path

from watchfiles import awatch

from agent_session._models import (
    AssistantMessage,
    AgentStatus,
    Event,
    event_status_to_agent_status,
)

logger = logging.getLogger(__name__)

_DEBOUNCE_MS = 300


def _parse_event_line(line: str) -> Event | None:
    line = line.strip()
    if not line:
        return None
    try:
        return Event.model_validate(json.loads(line))
    except (json.JSONDecodeError, ValueError):
        logger.warning("skipping malformed event line: %s", line)
        return None


def _read_new_lines(path: Path, offset: int) -> tuple[int, list[str]]:
    """Read complete text lines from *path* starting at byte *offset*.

    Returns ``(new_offset, lines)`` of the decoded, non-blank complete lines.
    Re-opens the file each call (rather than holding a handle) so it survives
    the file being **replaced** -- which is how Mutagen delivers mirror updates
    (atomic stage-and-rename, a new inode each sync). A held handle would keep
    pointing at the old, now-unlinked inode and miss every line after the first.
    Byte offsets work because the logs we tail (``events.jsonl`` /
    ``outbox.jsonl``) are append-only, so the mirror's content prefix is stable.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return offset, []
    # File shrank => it was replaced/truncated; re-read from the start.
    if size < offset:
        offset = 0
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return offset, []
    nl = data.rfind(b"\n")
    if nl == -1:
        return offset, []  # no complete line yet
    consumed = data[: nl + 1]
    lines = [
        raw.decode("utf-8", errors="replace")
        for raw in consumed.split(b"\n")
        if raw.strip()
    ]
    return offset + len(consumed), lines


def _read_new(path: Path, offset: int) -> tuple[int, list[Event]]:
    """Read complete event lines from *path*, parsing each into an ``Event``."""
    new_offset, lines = _read_new_lines(path, offset)
    events = [e for line in lines if (e := _parse_event_line(line)) is not None]
    return new_offset, events


async def _watch_raw(
    path: Path,
    *,
    tail: bool = True,
    stop_event: asyncio.Event | None = None,
) -> AsyncGenerator[str]:
    """Yield complete text lines as they are appended to *path*.

    Uses OS-native file notifications (kqueue/inotify) via *watchfiles* so lines
    are yielded as soon as the file is written to. Reads are by byte offset with
    a fresh open each time, so a file replaced atomically (e.g. a Mutagen mirror
    sync) is followed correctly, not just local in-place appends. This is the
    shared tail engine behind both :func:`watch_events` (events.jsonl) and
    :func:`watch_jsonl` (outbox.jsonl).

    Args:
        path: Path to the append-only JSONL file.
        tail: If ``True`` (default), skip existing lines and only yield new
            ones.  If ``False``, replay the entire file first.
        stop_event: Optional ``asyncio.Event`` to stop watching.
    """
    path = path.resolve()
    parent = path.parent

    # Watch the parent directory for all changes (more reliable than
    # watching a single file, especially on macOS temp dirs).
    def _is_our_file(changes: set[tuple[object, str]]) -> bool:
        return any(Path(p).resolve() == path for _, p in changes)

    # Wait for the file to appear.
    file_just_appeared = False
    if not await asyncio.to_thread(path.exists):
        async for changes in awatch(parent, stop_event=stop_event, debounce=_DEBOUNCE_MS):
            if _is_our_file(changes):
                if await asyncio.to_thread(path.exists):
                    file_just_appeared = True
                    break
        else:
            return

    if tail and not file_just_appeared:
        # Skip content that existed before we started watching.
        offset = await asyncio.to_thread(lambda: path.stat().st_size)
    else:
        # Replay everything (or all of a just-appeared file).
        offset, lines = await asyncio.to_thread(_read_new_lines, path, 0)
        for line in lines:
            yield line

    async for changes in awatch(parent, stop_event=stop_event, debounce=_DEBOUNCE_MS):
        if not _is_our_file(changes):
            continue
        offset, lines = await asyncio.to_thread(_read_new_lines, path, offset)
        for line in lines:
            yield line


async def watch_events(
    path: Path,
    *,
    tail: bool = True,
    stop_event: asyncio.Event | None = None,
) -> AsyncGenerator[Event]:
    """Yield ``Event`` objects as they are appended to *path*.

    Thin mapping layer over :func:`_watch_raw`: each complete line is parsed as
    an event; malformed lines are skipped (logged) rather than ending the watch.
    """
    async for line in _watch_raw(path, tail=tail, stop_event=stop_event):
        event = _parse_event_line(line)
        if event is not None:
            yield event


async def watch_jsonl(
    path: Path,
    *,
    tail: bool = True,
    stop_event: asyncio.Event | None = None,
) -> AsyncGenerator[dict]:
    """Yield JSON objects as they are appended to an append-only JSONL *path*.

    The message-plane outbox (``outbox.jsonl``) is tailed with this: it shares
    the offset/inode-safe engine of :func:`watch_events` (so it follows a
    bind-mounted or Mutagen-mirrored file identically) but yields raw dicts
    instead of typed events. Malformed or non-object lines are skipped.
    """
    async for line in _watch_raw(path, tail=tail, stop_event=stop_event):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("skipping malformed jsonl line: %s", line)
            continue
        if isinstance(obj, dict):
            yield obj


class EventMonitor:
    """Watches an events.jsonl file and maintains session state."""

    def __init__(self, events_path: Path) -> None:
        self._events_path = events_path
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

        self._session_id: str | None = None
        self._status: AgentStatus | None = None
        self._background_tasks = 0
        self._session_crons = 0
        self._transcript_path: Path | None = None
        self._ever_active = False
        self._ended = False
        self._assistant_messages: deque[AssistantMessage] = deque(maxlen=50)
        self._waiters: list[tuple[str, asyncio.Future[Event]]] = []

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def status(self) -> AgentStatus | None:
        return self._status

    @property
    def background_tasks(self) -> int:
        return self._background_tasks

    @property
    def session_crons(self) -> int:
        return self._session_crons

    @property
    def transcript_path(self) -> Path | None:
        return self._transcript_path

    @property
    def is_active(self) -> bool:
        return self._session_id is not None

    @property
    def ended(self) -> bool:
        """True once the agent reported a session-end (ENDED) event."""
        return self._ended

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def wait_for_event(self, event_name: str, timeout: float) -> Event:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Event] = loop.create_future()
        self._waiters.append((event_name, future))
        return await asyncio.wait_for(future, timeout=timeout)

    async def _run(self) -> None:
        try:
            async for event in watch_events(
                self._events_path, tail=True, stop_event=self._stop_event
            ):
                self._process_event(event)
        except asyncio.CancelledError:
            pass

    def get_assistant_messages(self, last: int = 1) -> list[AssistantMessage]:
        msgs = list(self._assistant_messages)
        return msgs[-last:] if last < len(msgs) else msgs

    def _process_event(self, event: Event) -> None:
        # Subagent lifecycle events describe a sub-lifecycle, not the main
        # session's user-facing state. Top-level Claude Code emits SubagentStop
        # ~2s after Stop on every turn, which would otherwise stomp the idle
        # status back to working.
        if event.event not in ("SubagentStart", "SubagentStop"):
            agent_status = event_status_to_agent_status(event.status)
            if agent_status is None:
                self._session_id = None
                self._status = None
                self._background_tasks = 0
                self._session_crons = 0
                self._ended = True
            else:
                self._session_id = event.session_id
                self._status = agent_status
                self._background_tasks = event.background_tasks
                self._session_crons = event.session_crons
                self._ever_active = True
                # A new active event supersedes any prior session-end (e.g.
                # a /clear that ends one session and starts another).
                self._ended = False
                if event.transcript_path is not None:
                    self._transcript_path = event.transcript_path

        if event.assistant_message is not None:
            self._assistant_messages.append(
                AssistantMessage(
                    message=event.assistant_message,
                    timestamp=event.timestamp,
                )
            )

        # Resolve matching waiters.
        remaining: list[tuple[str, asyncio.Future[Event]]] = []
        for name, future in self._waiters:
            if name == event.event and not future.done():
                future.set_result(event)
            else:
                remaining.append((name, future))
        self._waiters = remaining
