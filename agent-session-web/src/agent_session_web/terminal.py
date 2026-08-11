"""PTY <-> WebSocket bridge for a live, interactive tmux terminal.

This is the only place tmux is used directly, and only on a name `asn` already
gave us. We attach via a per-viewer *grouped* tmux session
(``agent-session-web-<key8>-<rand>``) so a second viewer/native attach cannot clamp the
window size of the browser terminal. Killing a grouped member never kills the
underlying asn session.

No blocking I/O on the event loop: the PTY master fd is non-blocking and driven
by ``loop.add_reader`` / ``loop.add_writer``.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import secrets
import struct
import termios
from dataclasses import dataclass

from starlette.websockets import WebSocket

#: Close codes.
WS_CLOSE_GONE = 4404
WS_CLOSE_ERROR = 1011

_READ_CHUNK = 65536


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """Apply terminal dimensions to a PTY via TIOCSWINSZ."""
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _grouped_member_name(session_key: str) -> str:
    """A unique grouped-session member name for one viewer."""
    return f"agent-session-web-{session_key[:8]}-{secrets.token_hex(3)}"


def _handle_control_frame(master_fd: int, text: str) -> bool:
    """Handle a text control frame. Returns True if it was a known control."""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    match payload:
        case {"type": "resize", "cols": int() as cols, "rows": int() as rows}:
            _set_winsize(master_fd, rows=rows, cols=cols)
            return True
        case _:
            return False


def _wake(fut: asyncio.Future[None]) -> None:
    if not fut.done():
        fut.set_result(None)


async def _pty_write_all(master_fd: int, data: bytes) -> None:
    """Write all bytes to a non-blocking PTY master, draining via add_writer."""
    loop = asyncio.get_running_loop()
    view = memoryview(data)
    while view:
        try:
            written = os.write(master_fd, view)
            view = view[written:]
        except (BlockingIOError, InterruptedError):
            fut: asyncio.Future[None] = loop.create_future()
            loop.add_writer(master_fd, _wake, fut)
            try:
                await fut
            finally:
                loop.remove_writer(master_fd)


@dataclass
class PtyHandle:
    """A spawned `tmux attach` process plus its PTY master fd."""

    proc: asyncio.subprocess.Process
    master_fd: int
    member_name: str | None


async def _tmux(*args: str) -> int:
    """Run a fire-and-forget tmux command, returning its exit code."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait()


#: Mission-control HUD recolour for the per-viewer tmux status line. These are
#: all *session*-scoped options, so they touch only our grouped member and never
#: leak to the underlying asn session or a native attach.
_HUD_TMUX_OPTS: tuple[tuple[str, str], ...] = (
    ("status-style", "bg=#0b1320,fg=#6b7d93"),
    ("status-left-style", "fg=#22d3ee,bold"),
    ("status-right-style", "fg=#475569"),
    ("message-style", "bg=#0b1320,fg=#22d3ee"),
    ("message-command-style", "bg=#0b1320,fg=#22d3ee"),
    ("mode-style", "bg=#13213a,fg=#22d3ee"),
    ("window-status-current-style", "fg=#22d3ee,bold"),
    ("window-status-style", "fg=#6b7d93"),
)


async def _apply_hud_theme(member: str) -> None:
    """Style only our grouped member's status line to match the web HUD."""
    for name, value in _HUD_TMUX_OPTS:
        await _tmux("set-option", "-t", member, name, value)


async def _new_grouped_session(tmux_session_name: str, member: str) -> bool:
    """Create a grouped session sharing windows with the asn session."""
    rc = await _tmux("new-session", "-d", "-t", tmux_session_name, "-s", member)
    if rc == 0:
        await _apply_hud_theme(member)
    return rc == 0


async def _kill_tmux_session(name: str) -> None:
    await _tmux("kill-session", "-t", name)


async def sweep_orphan_sessions() -> None:
    """Kill leftover ``agent-session-web-*`` grouped sessions from a previous run."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "list-sessions",
        "-F",
        "#{session_name}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return
    for line in out.decode(errors="replace").splitlines():
        if line.startswith("agent-session-web-"):
            await _kill_tmux_session(line)


async def spawn_tmux_attach(
    tmux_session_name: str,
    session_key: str,
    *,
    cols: int = 80,
    rows: int = 24,
) -> PtyHandle:
    """Open a PTY and run `tmux attach` to a per-viewer grouped session."""
    member = _grouped_member_name(session_key)
    created = await _new_grouped_session(tmux_session_name, member)
    target = member if created else tmux_session_name
    member_name = member if created else None

    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, rows=rows, cols=cols)
    env = dict(os.environ, TERM="xterm-256color")
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "attach",
            "-t",
            target,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            env=env,
        )
    finally:
        os.close(slave_fd)
    os.set_blocking(master_fd, False)
    return PtyHandle(proc=proc, master_fd=master_fd, member_name=member_name)


class _Bridge:
    """Pumps bytes between one WebSocket and one PTY master fd."""

    def __init__(self, websocket: WebSocket, handle: PtyHandle) -> None:
        self.ws = websocket
        self.handle = handle
        self.master_fd = handle.master_fd
        self.loop = asyncio.get_running_loop()
        self.out_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._reader_added = False
        self._closed = False

    def _on_readable(self) -> None:
        try:
            data = os.read(self.master_fd, _READ_CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:  # EOF — tmux detached / session ended
            self.out_queue.put_nowait(None)
            self._remove_reader()
            return
        self.out_queue.put_nowait(data)

    def _remove_reader(self) -> None:
        if self._reader_added:
            self.loop.remove_reader(self.master_fd)
            self._reader_added = False

    async def _pty_to_ws(self) -> None:
        while True:
            data = await self.out_queue.get()
            if data is None:
                break
            try:
                await self.ws.send_bytes(data)
            except Exception:
                break

    async def _ws_to_pty(self) -> None:
        while True:
            try:
                message = await self.ws.receive()
            except Exception:
                break
            match message:
                case {"type": "websocket.disconnect"}:
                    break
                case {"bytes": bytes() as data}:
                    await _pty_write_all(self.master_fd, data)
                case {"text": str() as text}:
                    _handle_control_frame(self.master_fd, text)
                case _:
                    continue

    async def run(self) -> None:
        self.loop.add_reader(self.master_fd, self._on_readable)
        self._reader_added = True
        sender = asyncio.create_task(self._pty_to_ws())
        receiver = asyncio.create_task(self._ws_to_pty())
        try:
            await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (sender, receiver):
                task.cancel()
            for task in (sender, receiver):
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            await self._cleanup()

    async def _cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True

        self._remove_reader()
        try:
            self.loop.remove_writer(self.master_fd)
        except (ValueError, OSError):
            pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass

        proc = self.handle.proc
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except ProcessLookupError:
                    pass

        if self.handle.member_name:
            await _kill_tmux_session(self.handle.member_name)

        try:
            await self.ws.close()
        except Exception:
            pass


async def bridge_session(
    websocket: WebSocket,
    tmux_session_name: str,
    session_key: str,
    *,
    cols: int = 80,
    rows: int = 24,
) -> None:
    """Attach to the session's tmux and bridge it to the WebSocket until close."""
    handle = await spawn_tmux_attach(
        tmux_session_name, session_key, cols=cols, rows=rows
    )
    bridge = _Bridge(websocket, handle)
    await bridge.run()
