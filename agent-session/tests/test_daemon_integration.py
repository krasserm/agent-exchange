"""Integration tests for daemon + client (real tmux, fake Claude Code events)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from pathlib import Path

import libtmux
import pytest

from agent_session._daemon import DaemonServer, SOCKET_PATH, PID_PATH
from conftest import make_event_line

_DETECT_DELAY = 1.0


@pytest.fixture
def dev_root(tmp_path: Path) -> Path:
    root = tmp_path / "Development"
    root.mkdir()
    return root


@pytest.fixture
def project_name() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def start_dir(dev_root: Path, project_name: str) -> Path:
    d = dev_root / project_name
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def set_dev_root_env(dev_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_SESSION_DEV_ROOT", str(dev_root))


@pytest.fixture
def tmux_server() -> libtmux.Server:
    return libtmux.Server()


@pytest.fixture
def session_name_prefix(project_name: str) -> str:
    return f"cc-{project_name}"


@pytest.fixture(autouse=True)
def cleanup_tmux(tmux_server: libtmux.Server, session_name_prefix: str) -> None:
    def _kill_matching() -> None:
        for s in tmux_server.sessions:
            if s.session_name.startswith(session_name_prefix):
                try:
                    s.kill()
                except Exception:
                    pass

    _kill_matching()
    yield
    _kill_matching()


@pytest.fixture
async def daemon_and_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Start an in-process daemon server and return a connected client."""
    # Use temp paths for daemon files to avoid interfering with real daemon.
    test_daemon_dir = tmp_path / "daemon"
    test_daemon_dir.mkdir()
    # Use /tmp for the socket to avoid macOS AF_UNIX path length limit (104 bytes).
    test_socket = Path(f"/tmp/cs-test-{uuid.uuid4().hex[:8]}.sock")
    test_pid = test_daemon_dir / "daemon.pid"
    test_sessions = test_daemon_dir / "sessions"

    monkeypatch.setattr("agent_session._naming.DAEMON_DIR", test_daemon_dir)
    monkeypatch.setattr("agent_session._naming.SESSIONS_DIR", test_sessions)
    monkeypatch.setattr("agent_session._daemon.SOCKET_PATH", test_socket)
    monkeypatch.setattr("agent_session._daemon.PID_PATH", test_pid)
    monkeypatch.setattr("agent_session._client.SOCKET_PATH", test_socket)

    server = DaemonServer()
    # Disable auto-shutdown so daemon stays alive for the test.
    original_handle_stop = server._handle_stop
    original_handle_stop_all = server._handle_stop_all

    async def _handle_stop_no_shutdown(params):
        result = await original_handle_stop(params)
        server._should_shutdown = False
        return result

    async def _handle_stop_all_no_shutdown():
        result = await original_handle_stop_all()
        server._should_shutdown = False
        return result

    server._handle_stop = _handle_stop_no_shutdown
    server._handle_stop_all = _handle_stop_all_no_shutdown

    server._server = await asyncio.start_unix_server(
        server._handle_connection, path=str(test_socket),
    )

    from agent_session._client import DaemonClient
    client = DaemonClient()

    yield server, client, test_sessions

    # Cleanup: stop all sessions, close server.
    for key in list(server._registry.all_keys()):
        session = server._registry.get(key)
        await session.stop()
        server._registry.remove(key)
    server._server.close()
    await server._server.wait_closed()
    if test_socket.exists():
        test_socket.unlink()


def _append_event(session_dir: Path, event_name: str, session_id: str = "s1", **extra: str) -> None:
    events_path = session_dir / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a") as f:
        f.write(make_event_line(event_name, session_id=session_id, **extra) + "\n")


async def _acking_agent(session_dir: Path, interval: float = 0.4):
    """Background stand-in for the agent's UserPromptSubmit hook.

    ``send_user_message`` waits for that ack before returning, so a fake agent
    that never emits one now makes every send raise. Appending on an interval
    (rather than once per send) keeps the helper immune to which send is in
    flight when the event lands.
    """
    async def _run() -> None:
        while True:
            _append_event(session_dir, "UserPromptSubmit")
            await asyncio.sleep(interval)

    task = asyncio.create_task(_run())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _start_session(
    client, start_dir: Path, sessions_dir: Path, timeout: float = 10.0,
) -> dict:
    """Start a session and emit a fake SessionStart event."""
    # Snapshot existing session dirs so we can detect the new one.
    existing = set(sessions_dir.iterdir()) if sessions_dir.exists() else set()

    result_holder = {}
    error_holder = {}

    async def do_start():
        try:
            result = await client.send("start", {
                "start_dir": str(start_dir),
                "skip_permissions": False,
                "timeout": timeout,
            })
            result_holder["result"] = result
        except Exception as e:
            error_holder["error"] = e

    start_task = asyncio.create_task(do_start())

    # Wait for a NEW session dir to appear, then emit a fake SessionStart event.
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.2)
        if sessions_dir.exists():
            new_dirs = set(sessions_dir.iterdir()) - existing
            if new_dirs:
                session_dir = new_dirs.pop()
                _append_event(session_dir, "SessionStart")
                break
    else:
        start_task.cancel()
        raise TimeoutError("Session dir never appeared")

    await start_task
    if "error" in error_holder:
        raise error_holder["error"]
    return result_holder["result"]


class TestDaemonStartStop:
    async def test_start_returns_session_key(self, daemon_and_client, start_dir: Path) -> None:
        server, client, sessions_dir = daemon_and_client
        result = await _start_session(client, start_dir, sessions_dir)

        assert "session_key" in result
        assert len(result["session_key"]) == 32
        assert "tmux_session_name" in result

    async def test_stop_removes_session(self, daemon_and_client, start_dir: Path) -> None:
        server, client, sessions_dir = daemon_and_client
        result = await _start_session(client, start_dir, sessions_dir)
        key = result["session_key"]

        await client.send("stop", {"key": key})

        sessions = await client.send("list")
        assert len(sessions) == 0

    async def test_stop_with_prefix(self, daemon_and_client, start_dir: Path) -> None:
        server, client, sessions_dir = daemon_and_client
        result = await _start_session(client, start_dir, sessions_dir)
        key = result["session_key"]

        await client.send("stop", {"key": key[:3]})

        sessions = await client.send("list")
        assert len(sessions) == 0

    async def test_stop_nonexistent_raises(self, daemon_and_client) -> None:
        _, client, _ = daemon_and_client
        from agent_session._client import DaemonError
        with pytest.raises(DaemonError, match="No session matching"):
            await client.send("stop", {"key": "nonexistent"})

    async def test_stop_all(self, daemon_and_client, dev_root: Path) -> None:
        server, client, sessions_dir = daemon_and_client

        dir1 = dev_root / f"proj-{uuid.uuid4().hex[:8]}"
        dir1.mkdir()
        dir2 = dev_root / f"proj-{uuid.uuid4().hex[:8]}"
        dir2.mkdir()

        await _start_session(client, dir1, sessions_dir)
        await _start_session(client, dir2, sessions_dir)

        sessions = await client.send("list")
        assert len(sessions) == 2

        await client.send("stop_all")

        sessions = await client.send("list")
        assert len(sessions) == 0


class TestDaemonListStatus:
    async def test_list_empty(self, daemon_and_client) -> None:
        _, client, _ = daemon_and_client
        sessions = await client.send("list")
        assert sessions == []

    async def test_list_shows_sessions(self, daemon_and_client, start_dir: Path) -> None:
        server, client, sessions_dir = daemon_and_client
        result = await _start_session(client, start_dir, sessions_dir)

        sessions = await client.send("list")
        assert len(sessions) == 1
        assert sessions[0]["session_key"] == result["session_key"]
        assert sessions[0]["status"] == "idle"

    async def test_status_shows_details(self, daemon_and_client, start_dir: Path) -> None:
        server, client, sessions_dir = daemon_and_client
        result = await _start_session(client, start_dir, sessions_dir)
        key = result["session_key"]

        status = await client.send("status", {"key": key})
        assert status["session_key"] == key
        assert status["status"] == "idle"
        assert status["session_id"] == "s1"
        assert status["start_dir"] == str(start_dir.resolve())
        assert status["tmux_session_name"] is not None

    async def test_status_with_prefix(self, daemon_and_client, start_dir: Path) -> None:
        server, client, sessions_dir = daemon_and_client
        result = await _start_session(client, start_dir, sessions_dir)
        key = result["session_key"]

        status = await client.send("status", {"key": key[:4]})
        assert status["session_key"] == key


class TestDaemonSendCapture:
    async def test_send_message(self, daemon_and_client, start_dir: Path) -> None:
        server, client, sessions_dir = daemon_and_client
        result = await _start_session(client, start_dir, sessions_dir)
        key = result["session_key"]

        # Send should not raise for an active session.
        async with contextlib.aclosing(_acking_agent(sessions_dir / key)) as agent:
            await anext(agent)
            await client.send("send", {"key": key, "message": "hello"})

    async def test_capture(self, daemon_and_client, start_dir: Path) -> None:
        server, client, sessions_dir = daemon_and_client
        result = await _start_session(client, start_dir, sessions_dir)
        key = result["session_key"]

        result = await client.send("capture", {"key": key})
        assert "content" in result
        assert isinstance(result["content"], str)

    async def test_send_to_ended_session(self, daemon_and_client, start_dir: Path) -> None:
        server, client, sessions_dir = daemon_and_client
        result = await _start_session(client, start_dir, sessions_dir)
        key = result["session_key"]

        # Emit SessionEnd
        session_dir = sessions_dir / key
        _append_event(session_dir, "SessionEnd", session_id="s1")
        await asyncio.sleep(_DETECT_DELAY)

        from agent_session._client import DaemonError
        with pytest.raises(DaemonError, match="No active agent session"):
            await client.send("send", {"key": key, "message": "hello"})

    async def test_concurrent_sends_serialized(self, daemon_and_client, start_dir: Path) -> None:
        """Two concurrent sends to the same session should not error."""
        server, client, sessions_dir = daemon_and_client
        result = await _start_session(client, start_dir, sessions_dir)
        key = result["session_key"]

        async with contextlib.aclosing(_acking_agent(sessions_dir / key)) as agent:
            await anext(agent)
            await asyncio.gather(
                client.send("send", {"key": key, "message": "first"}),
                client.send("send", {"key": key, "message": "second"}),
            )


class TestDaemonMessages:
    async def test_messages(self, daemon_and_client, start_dir: Path) -> None:
        server, client, sessions_dir = daemon_and_client
        result = await _start_session(client, start_dir, sessions_dir)
        key = result["session_key"]

        session_dir = sessions_dir / key
        _append_event(
            session_dir, "Stop", session_id="s1",
            assistant_message="Hello from daemon test",
        )
        await asyncio.sleep(_DETECT_DELAY)

        result = await client.send("messages", {"key": key, "last": 1})
        assert "messages" in result
        assert len(result["messages"]) == 1
        assert result["messages"][0]["message"] == "Hello from daemon test"
        assert "timestamp" in result["messages"][0]

    async def test_messages_empty(self, daemon_and_client, start_dir: Path) -> None:
        server, client, sessions_dir = daemon_and_client
        result = await _start_session(client, start_dir, sessions_dir)
        key = result["session_key"]

        result = await client.send("messages", {"key": key})
        assert result["messages"] == []

    async def test_request_method_removed(self, daemon_and_client, start_dir: Path) -> None:
        server, client, sessions_dir = daemon_and_client
        result = await _start_session(client, start_dir, sessions_dir)
        key = result["session_key"]

        from agent_session._client import DaemonError
        with pytest.raises(DaemonError, match="Unknown method"):
            await client.send("request", {"key": key, "message": "hello"})


class TestDaemonMetaJson:
    async def test_meta_written_on_start(self, daemon_and_client, start_dir: Path, tmp_path: Path) -> None:
        server, client, sessions_dir = daemon_and_client
        result = await _start_session(client, start_dir, sessions_dir)
        key = result["session_key"]

        # Check meta.json exists in the daemon's sessions dir.
        meta_file = sessions_dir / key / "meta.json"
        assert meta_file.exists()

        meta = json.loads(meta_file.read_text())
        assert meta["session_key"] == key
        assert meta["start_dir"] == str(start_dir.resolve())

    async def test_meta_removed_on_stop(self, daemon_and_client, start_dir: Path, tmp_path: Path) -> None:
        server, client, sessions_dir = daemon_and_client
        result = await _start_session(client, start_dir, sessions_dir)
        key = result["session_key"]

        meta_file = sessions_dir / key / "meta.json"
        assert meta_file.exists()

        await client.send("stop", {"key": key})
        assert not meta_file.exists()


class TestStartupServesDuringOrphanRecovery:
    """The socket must be live before the orphan reaps run (issue #4).

    Recovery used to be awaited *before* the socket bind and the PID write, so a
    single unreachable host's ssh reap (30s) stalled startup past the client's
    5s budget -- and with no PID file yet, every other client concluded no daemon
    was running, spawned its own, lost the flock and exited silently. That is the
    9/9 "Failed to connect to daemon after starting it".
    """

    async def test_serves_requests_while_reap_is_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import threading
        import time

        daemon_dir = tmp_path / "daemon"
        sessions = daemon_dir / "sessions"
        sock = Path(f"/tmp/cs-test-{uuid.uuid4().hex[:8]}.sock")

        monkeypatch.setattr("agent_session._naming.DAEMON_DIR", daemon_dir)
        monkeypatch.setattr("agent_session._naming.SESSIONS_DIR", sessions)
        monkeypatch.setattr("agent_session._daemon.SOCKET_PATH", sock)
        monkeypatch.setattr("agent_session._daemon.PID_PATH", daemon_dir / "daemon.pid")
        monkeypatch.setattr("agent_session._daemon.LOCK_PATH", daemon_dir / "daemon.lock")
        monkeypatch.setattr("agent_session._client.SOCKET_PATH", sock)

        # An orphan whose host is unreachable: its reap is the slow part.
        meta_dir = sessions / "deadbeef"
        meta_dir.mkdir(parents=True)
        (meta_dir / "meta.json").write_text(json.dumps({
            "session_key": "deadbeef",
            "tmux_session_name": "cc-orphan-deadbeef",
            "transport": {"location": "remote", "host": "192.0.2.1",
                          "runtime": "container", "container": "cx-deadbeef"},
            "remote_session_name": "cx-deadbeef",
        }))

        reap_started = threading.Event()
        reap_finished = threading.Event()

        def slow_reap(meta: dict) -> None:
            # Blocking on purpose: if the daemon runs this on the event loop the
            # socket accepts but no request is ever served.
            reap_started.set()
            time.sleep(3.0)
            reap_finished.set()

        monkeypatch.setattr("agent_session._daemon._reap_remote_from_meta", slow_reap)

        from agent_session._client import DaemonClient
        from agent_session._daemon import DaemonServer

        server = DaemonServer()
        run_task = asyncio.create_task(server.run())
        try:
            # The reap must start without being awaited before the bind.
            await asyncio.wait_for(
                asyncio.to_thread(reap_started.wait, 5.0), timeout=6.0
            )
            assert not reap_finished.is_set()

            client = DaemonClient()
            result = await asyncio.wait_for(
                client.send("list", {}, timeout=2.0), timeout=2.0
            )
            assert result == []
            # Still in flight: the request was served *during* the reap.
            assert not reap_finished.is_set()
            assert (daemon_dir / "daemon.pid").exists()
        finally:
            # close() makes serve_forever() raise CancelledError; that is how the
            # daemon's own signal handler stops it, so expect it here too.
            server._request_shutdown()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(run_task, timeout=15.0)
            sock.unlink(missing_ok=True)
