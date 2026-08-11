"""Unit tests for daemon transport plumbing: meta persistence + remote reaping.

Also covers the message-plane read endpoint: token gating, the method
whitelist, and that it reuses the existing read handlers.
"""

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_session import _daemon
from agent_session._daemon import DaemonServer, SessionRegistry, _reap_remote_from_meta
from agent_session._models import AgentInfo, AgentStatus, AssistantMessage
from agent_session._session import AgentSession
from agent_session.agents import LaunchSpec, Transport
from agent_session.agents.claude import ClaudeAgent


@pytest.fixture(autouse=True)
def sessions_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "sessions"
    monkeypatch.setattr(_daemon._naming, "SESSIONS_DIR", d)
    return d


class TestMetaPersistence:
    def test_meta_includes_transport_and_remote_name(self, sessions_dir: Path) -> None:
        session = AgentSession(
            ClaudeAgent(),
            LaunchSpec(
                start_dir=Path("/srv/proj"),
                transport=Transport(location="remote", host="192.168.94.50", runtime="container", container="cc-x"),
            ),
        )
        DaemonServer()._write_meta(session)
        meta = json.loads((sessions_dir / session.session_key / "meta.json").read_text())
        assert meta["transport"] == {
            "location": "remote",
            "host": "192.168.94.50",
            "runtime": "container",
            "container": "cc-x",
        }
        assert meta["remote_session_name"] == session.remote_session_name

    def test_local_native_meta_marks_native(self, sessions_dir: Path) -> None:
        session = AgentSession(ClaudeAgent(), LaunchSpec(start_dir=Path.home()))
        DaemonServer()._write_meta(session)
        meta = json.loads((sessions_dir / session.session_key / "meta.json").read_text())
        assert meta["transport"]["location"] == "local"
        assert meta["transport"]["runtime"] == "native"
        assert meta["remote_session_name"] is None

    def test_meta_persists_read_token(self, sessions_dir: Path) -> None:
        session = AgentSession(ClaudeAgent(), LaunchSpec(start_dir=Path.home()))
        DaemonServer()._write_meta(session)
        meta = json.loads((sessions_dir / session.session_key / "meta.json").read_text())
        assert meta["read_token"] == session.read_token
        assert len(session.read_token) >= 16


class _FakeSession:
    """Minimal stand-in registered for read-endpoint tests (no tmux/agent)."""

    def __init__(self, key: str, token: str, message: str = "peer reply") -> None:
        self.session_key = key
        self.read_token = token
        self.agent_name = "claude"
        self.start_dir = Path("/proj")
        self.project_dir = Path("/proj")
        self.transport = Transport()
        self._message = message

    async def get_assistant_messages(self, last: int = 1) -> list[AssistantMessage]:
        return [
            AssistantMessage(
                message=self._message, timestamp=datetime.now(timezone.utc)
            )
        ]

    async def get_agent_info(self) -> AgentInfo:
        return AgentInfo(session_id="s1", status=AgentStatus.IDLE, transcript_path=None)

    async def get_tmux_session_name(self) -> str:
        return "cc-fake"

    async def capture(self, lines: int = 50) -> str:
        return "PANE TEXT"


async def _read_rpc(server: DaemonServer, payload: dict) -> dict:
    """Drive ``_handle_read_connection`` over a real ephemeral TCP socket."""
    srv = await asyncio.start_server(
        server._handle_read_connection, host="127.0.0.1", port=0
    )
    host, port = srv.sockets[0].getsockname()[:2]
    try:
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        line = await reader.readline()
        writer.close()
        await writer.wait_closed()
        return json.loads(line.decode())
    finally:
        srv.close()
        await srv.wait_closed()


class TestOutboxWatcher:
    """The daemon tails each session's outbox and dispatches only ``send``."""

    async def test_appended_line_dispatches_send(self, tmp_path: Path) -> None:
        server = DaemonServer()
        calls: list[dict] = []

        async def fake_send(params: dict) -> None:
            calls.append(params)

        server._handle_send = fake_send  # type: ignore[method-assign]

        outbox = tmp_path / "outbox.jsonl"
        outbox.write_text("")
        sess = _FakeSession("self00000000", "tok")
        sess.outbox_path = outbox  # type: ignore[attr-defined]
        server._start_outbox_watcher(sess)
        try:
            await asyncio.sleep(1.0)  # let the watcher attach
            with outbox.open("a") as f:
                f.write(json.dumps({"to": "peerkey0000", "message": "hello"}) + "\n")
                # Lines lacking to/message must be ignored, not dispatched.
                f.write(json.dumps({"from": "x"}) + "\n")
            for _ in range(40):
                if calls:
                    break
                await asyncio.sleep(0.25)
        finally:
            await server._cancel_outbox_watcher("self00000000")

        assert calls == [{"key": "peerkey0000", "message": "hello"}]


class TestRegistryTokenIndex:
    def test_add_remove_lookup(self) -> None:
        reg = SessionRegistry()
        reg.add(_FakeSession("k1", "tok-1"))
        assert reg.key_for_token("tok-1") == "k1"
        assert reg.key_for_token("nope") is None
        assert reg.key_for_token(None) is None
        reg.remove("k1")
        assert reg.key_for_token("tok-1") is None


class TestReadEndpoint:
    @pytest.fixture
    def server_with_peer(self) -> tuple[DaemonServer, _FakeSession]:
        server = DaemonServer()
        peer = _FakeSession("peerkey0000", "good-token", message="hi from peer")
        server._registry.add(peer)
        return server, peer

    async def test_valid_token_messages(self, server_with_peer) -> None:
        server, peer = server_with_peer
        resp = await _read_rpc(server, {
            "token": "good-token", "method": "messages",
            "params": {"key": "peerkey0000", "last": 1},
        })
        assert resp["ok"] is True
        assert resp["result"]["messages"][0]["message"] == "hi from peer"

    async def test_status_and_capture_whitelisted(self, server_with_peer) -> None:
        server, _ = server_with_peer
        status = await _read_rpc(server, {
            "token": "good-token", "method": "status",
            "params": {"key": "peerkey0000"},
        })
        assert status["ok"] is True
        assert status["result"]["status"] == "idle"

        capture = await _read_rpc(server, {
            "token": "good-token", "method": "capture",
            "params": {"key": "peerkey0000"},
        })
        assert capture["ok"] is True
        assert capture["result"]["content"] == "PANE TEXT"

    async def test_wrong_token_rejected(self, server_with_peer) -> None:
        server, _ = server_with_peer
        resp = await _read_rpc(server, {
            "token": "bad-token", "method": "messages",
            "params": {"key": "peerkey0000"},
        })
        assert resp["ok"] is False
        assert "token" in resp["error"]

    async def test_missing_token_rejected(self, server_with_peer) -> None:
        server, _ = server_with_peer
        resp = await _read_rpc(server, {
            "method": "messages", "params": {"key": "peerkey0000"},
        })
        assert resp["ok"] is False
        assert "token" in resp["error"]

    async def test_non_whitelisted_method_refused(self, server_with_peer) -> None:
        # A valid token must still not reach lifecycle methods over the endpoint.
        server, _ = server_with_peer
        for method in ("stop", "start", "daemon_stop", "send"):
            resp = await _read_rpc(server, {
                "token": "good-token", "method": method,
                "params": {"key": "peerkey0000"},
            })
            assert resp["ok"] is False, method
            assert "not allowed" in resp["error"], method


class TestReapRemoteFromMeta:
    def test_local_native_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        _reap_remote_from_meta({
            "transport": {"location": "local", "runtime": "native"},
            "remote_session_name": None,
        })
        assert calls == []

    def test_remote_native_kills_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = []
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
        )
        _reap_remote_from_meta({
            "transport": {"location": "remote", "host": "h", "runtime": "native"},
            "remote_session_name": "cc-abcd1234",
        })
        assert len(calls) == 1
        joined = " ".join(calls[0])
        assert joined.startswith("bash -c ")
        assert "tmux kill-session -t cc-abcd1234" in joined
        assert "h" in joined

    def test_container_removes_container(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = []
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
        )
        _reap_remote_from_meta({
            "transport": {"location": "local", "host": None, "runtime": "container", "container": "cc-x"},
            "remote_session_name": "cc-x",
        })
        assert any("docker rm -f cc-x" in " ".join(c) for c in calls)
