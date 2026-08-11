"""Client for communicating with the daemon over a Unix socket.

Handles auto-starting the daemon on first use (tmux model).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_session import _naming
from agent_session._daemon import PID_PATH, SOCKET_PATH


class DaemonClient:
    """Connects to the daemon's Unix socket to send requests."""

    async def send(
        self, method: str, params: dict | None = None, timeout: float = 120.0,
        *, auto_start: bool = False,
    ) -> Any:
        """Send a request to the daemon and return the result.

        Auto-starts the daemon only when ``auto_start`` is True (i.e. for
        mutating calls like ``start``). Read-only and maintenance calls must
        not resurrect a dead daemon: a frequent poller (web UI, ``asn-top``)
        hitting a down daemon would otherwise spawn one on every tick, and
        concurrent spawns race to clobber each other's socket. Such calls
        raise ``DaemonNotRunningError`` instead.
        """
        try:
            return await self._send(method, params or {}, timeout)
        except (ConnectionRefusedError, FileNotFoundError):
            if not auto_start:
                raise DaemonNotRunningError(
                    "No agent-session daemon is running"
                )
            await self._ensure_daemon()
            return await self._send(method, params or {}, timeout)

    async def _send(
        self, method: str, params: dict, timeout: float,
    ) -> Any:
        reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
        try:
            request = json.dumps({"method": method, "params": params})
            writer.write(request.encode() + b"\n")
            await writer.drain()

            data = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not data:
                raise ConnectionError("Daemon closed connection without response")

            response = json.loads(data.decode())
            if not response.get("ok"):
                raise DaemonError(response.get("error", "Unknown error"))
            return response.get("result")
        finally:
            writer.close()
            await writer.wait_closed()

    async def _ensure_daemon(self) -> None:
        """Start the daemon if it isn't running, then wait for the socket."""
        if _is_daemon_running():
            # Daemon process exists but socket not ready yet -- wait.
            await self._wait_for_socket()
            return

        _start_daemon_process()
        await self._wait_for_socket()

    async def _wait_for_socket(self, timeout: float = 5.0) -> None:
        """Wait for the daemon socket to become connectable."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                reader, writer = await asyncio.open_unix_connection(
                    str(SOCKET_PATH),
                )
                writer.close()
                await writer.wait_closed()
                return
            except (ConnectionRefusedError, FileNotFoundError):
                await asyncio.sleep(0.1)
        raise RuntimeError("Failed to connect to daemon after starting it")


class DaemonError(Exception):
    """Error returned by the daemon."""


class DaemonNotRunningError(DaemonError):
    """A daemon was required but none is running (and auto-start was not requested)."""


def _is_daemon_running() -> bool:
    if not PID_PATH.exists():
        return False
    try:
        pid = int(PID_PATH.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError, PermissionError):
        return False


def _start_daemon_process() -> None:
    _naming.DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _naming.DAEMON_DIR / "daemon.log"
    with open(log_path, "a") as log:
        subprocess.Popen(
            [sys.executable, "-m", "agent_session._daemon"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
