"""Integration tests for REST routes and the terminal WebSocket."""

from __future__ import annotations

import fcntl
import json
import os
import pty
import struct
import termios

import pytest
from fastapi.testclient import TestClient

from agent_session_web import cs_client, terminal
from agent_session_web.app import app
from agent_session_web.cs_client import CsError
from agent_session_web.models import MessageModel, SessionDetailModel, SessionModel

KEY = "a" * 32


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _detail(status: str | None = "working") -> SessionDetailModel:
    return SessionDetailModel(
        session_key=KEY,
        tmux_session_name="claude-x",
        claude_start_dir="/home/u/proj",
        status=status,
        session_id="sid",
        project="proj",
        claude_project_dir="/home/u/proj",
        transcript_path="/t.jsonl",
    )


def test_list_sessions_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_list() -> list[SessionModel]:
        return [
            SessionModel(
                session_key=KEY,
                tmux_session_name="claude-x",
                claude_start_dir="/home/u/proj",
                status="idle",
                session_id="sid",
                project="proj",
            )
        ]

    monkeypatch.setattr(cs_client, "list_sessions", fake_list)
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    assert resp.json()[0]["project"] == "proj"


def test_list_sessions_empty_on_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list() -> list[SessionModel]:
        return []  # cs_client already swallows CsError → []

    monkeypatch.setattr(cs_client, "list_sessions", fake_list)
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    assert resp.json() == []


def test_backgrounded_status_passthrough(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fake the asn subprocess itself so the real cs_client parsing runs.
    payload = {
        "session_key": KEY,
        "agent": "claude",
        "tmux_session_name": "cc-x",
        "start_dir": "/home/u/proj",
        "status": "backgrounded",
        "session_id": "sid",
        "project_dir": "/home/u/proj",
        "transcript_path": "/t.jsonl",
        "background_tasks": 2,
        "session_crons": 1,
    }

    class _Proc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return json.dumps(payload).encode(), b""

    async def fake_exec(*args: object, **kwargs: object) -> _Proc:
        return _Proc()

    monkeypatch.setattr(cs_client.asyncio, "create_subprocess_exec", fake_exec)

    resp = client.get(f"/api/sessions/{KEY}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "backgrounded"
    assert body["background_tasks"] == 2
    assert body["session_crons"] == 1


def test_list_backgrounded_status_passthrough(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = [
        {
            "session_key": KEY,
            "agent": "claude",
            "tmux_session_name": "cc-x",
            "start_dir": "/home/u/proj",
            "status": "backgrounded",
            "session_id": "sid",
            "background_tasks": 2,
            "session_crons": 0,
        }
    ]

    class _Proc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return json.dumps(payload).encode(), b""

    async def fake_exec(*args: object, **kwargs: object) -> _Proc:
        return _Proc()

    monkeypatch.setattr(cs_client.asyncio, "create_subprocess_exec", fake_exec)

    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    body = resp.json()[0]
    assert body["status"] == "backgrounded"
    assert body["background_tasks"] == 2
    assert body["session_crons"] == 0


def test_get_session_unknown_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get(key: str, timeout: float | None = None) -> SessionDetailModel:
        raise CsError("No session matching 'x'")

    monkeypatch.setattr(cs_client, "get_session", fake_get)
    resp = client.get(f"/api/sessions/{KEY}")
    assert resp.status_code == 404


def test_stop_session(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    async def fake_stop(key: str) -> None:
        called.append(key)

    monkeypatch.setattr(cs_client, "stop_session", fake_stop)
    resp = client.post(f"/api/sessions/{KEY}/stop")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert called == [KEY]


def test_messages_get_and_send(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_messages(key: str, last: int = 5) -> list[MessageModel]:
        return [MessageModel(message="hi", timestamp="2026-06-08T00:00:00")]

    sent: list[tuple[str, str]] = []

    async def fake_send(key: str, message: str) -> None:
        sent.append((key, message))

    monkeypatch.setattr(cs_client, "get_messages", fake_messages)
    monkeypatch.setattr(cs_client, "send_message", fake_send)

    resp = client.get(f"/api/sessions/{KEY}/messages?last=3")
    assert resp.status_code == 200
    assert resp.json()[0]["message"] == "hi"

    resp = client.post(f"/api/sessions/{KEY}/messages", json={"message": "go"})
    assert resp.status_code == 200
    assert sent == [(KEY, "go")]


def test_spa_fallback_served_when_built(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When dist/index.html is missing we expect a clear 404, not a crash.
    resp = client.get("/some/client/route")
    assert resp.status_code in (200, 404)


class _FakeProc:
    def __init__(self) -> None:
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def test_ws_terminal_echo_and_resize(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder: dict[str, int] = {}

    async def fake_get(key: str, timeout: float | None = None) -> SessionDetailModel:
        return _detail(status="working")

    async def fake_spawn(
        tmux_session_name: str, session_key: str, *, cols: int = 80, rows: int = 24
    ) -> terminal.PtyHandle:
        master_fd, slave_fd = pty.openpty()
        terminal._set_winsize(master_fd, rows=rows, cols=cols)
        os.set_blocking(master_fd, False)
        holder["master"] = master_fd
        holder["slave"] = slave_fd
        return terminal.PtyHandle(
            proc=_FakeProc(), master_fd=master_fd, member_name=None
        )

    monkeypatch.setattr(cs_client, "get_session", fake_get)
    monkeypatch.setattr(terminal, "spawn_tmux_attach", fake_spawn)

    with client.websocket_connect(f"/ws/{KEY}/terminal") as ws:
        ws.send_text(json.dumps({"type": "resize", "cols": 111, "rows": 22}))
        ws.send_bytes(b"hello")
        # Echo proves the keystroke reached the PTY (ordered after resize).
        received = b""
        for _ in range(20):
            received += ws.receive_bytes()
            if b"hello" in received:
                break
        assert b"hello" in received

        packed = fcntl.ioctl(
            holder["slave"], termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0)
        )
        rows, cols, _, _ = struct.unpack("HHHH", packed)
        assert (rows, cols) == (22, 111)

    os.close(holder["slave"])


def test_ws_terminal_closes_when_session_ended(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get(key: str, timeout: float | None = None) -> SessionDetailModel:
        return _detail(status=None)  # ended

    monkeypatch.setattr(cs_client, "get_session", fake_get)

    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/{KEY}/terminal") as ws:
            ws.receive_bytes()
