"""Unit tests for the async `asn` CLI wrapper."""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_session_web import cs_client
from agent_session_web.cs_client import CsError


class FakeProcess:
    """Stand-in for an asyncio subprocess returned by create_subprocess_exec."""

    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        hang: bool = False,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.Event().wait()  # never resolves
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def _patch_proc(monkeypatch: pytest.MonkeyPatch, proc: FakeProcess) -> list[list[str]]:
    """Patch create_subprocess_exec to return `proc`; record argv lists."""
    calls: list[list[str]] = []

    async def fake_exec(*args: str, **kwargs: object) -> FakeProcess:
        calls.append(list(args))
        return proc

    monkeypatch.setattr(cs_client.asyncio, "create_subprocess_exec", fake_exec)
    return calls


async def test_list_sessions_parses_json(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [
        {
            "session_key": "a" * 32,
            "tmux_session_name": "claude-abc",
            "claude_start_dir": "/Users/martin/Development/sandbox/agent-session-web",
            "status": "working",
            "session_id": "sid-1",
        }
    ]
    calls = _patch_proc(monkeypatch, FakeProcess(stdout=json.dumps(payload).encode()))

    sessions = await cs_client.list_sessions()

    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_key == "a" * 32
    assert s.tmux_session_name == "claude-abc"
    assert s.status == "working"
    assert s.project == "agent-session-web"  # basename label
    assert calls[0][1:] == ["list", "--json"]


async def test_list_sessions_reads_agent_session_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # agent-session (successor of agent-session) renamed claude_start_dir -> start_dir.
    payload = [
        {
            "session_key": "a" * 32,
            "agent": "claude",
            "tmux_session_name": "cc-abc",
            "start_dir": "/Users/martin/Development/krasserm/bewerbung",
            "status": "idle",
            "session_id": "sid-1",
        }
    ]
    _patch_proc(monkeypatch, FakeProcess(stdout=json.dumps(payload).encode()))

    sessions = await cs_client.list_sessions()

    assert sessions[0].project == "bewerbung"
    assert sessions[0].claude_start_dir == "/Users/martin/Development/krasserm/bewerbung"


async def test_list_sessions_reads_background_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        {
            "session_key": "a" * 32,
            "agent": "claude",
            "tmux_session_name": "cc-abc",
            "start_dir": "/home/u/proj",
            "status": "backgrounded",
            "session_id": "sid-1",
            "background_tasks": 2,
            "session_crons": 1,
        }
    ]
    _patch_proc(monkeypatch, FakeProcess(stdout=json.dumps(payload).encode()))

    sessions = await cs_client.list_sessions()

    assert sessions[0].status == "backgrounded"
    assert sessions[0].background_tasks == 2
    assert sessions[0].session_crons == 1


async def test_list_sessions_defaults_background_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Older asn output has no count fields; they must default to 0.
    payload = [
        {
            "session_key": "a" * 32,
            "tmux_session_name": "claude-abc",
            "claude_start_dir": "/home/u/proj",
            "status": "idle",
            "session_id": "sid-1",
        }
    ]
    _patch_proc(monkeypatch, FakeProcess(stdout=json.dumps(payload).encode()))

    sessions = await cs_client.list_sessions()

    assert sessions[0].background_tasks == 0
    assert sessions[0].session_crons == 0


async def test_list_sessions_reads_remote_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        {
            "session_key": "a" * 32,
            "agent": "claude",
            "tmux_session_name": "cc-abc",
            "start_dir": "/home/martin/Development/krasserm/dashboard",
            "host": "192.168.94.50",
            "status": "idle",
            "session_id": "sid-1",
        }
    ]
    _patch_proc(monkeypatch, FakeProcess(stdout=json.dumps(payload).encode()))

    sessions = await cs_client.list_sessions()

    assert sessions[0].host == "192.168.94.50"


async def test_list_sessions_defaults_host_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Local sessions report host: null; older asn output omits the key entirely.
    payload = [
        {
            "session_key": "a" * 32,
            "agent": "claude",
            "tmux_session_name": "cc-abc",
            "start_dir": "/home/u/proj",
            "host": None,
            "status": "idle",
            "session_id": "sid-1",
        },
        {
            "session_key": "b" * 32,
            "tmux_session_name": "claude-abc",
            "claude_start_dir": "/home/u/proj",
            "status": "idle",
            "session_id": "sid-2",
        },
    ]
    _patch_proc(monkeypatch, FakeProcess(stdout=json.dumps(payload).encode()))

    sessions = await cs_client.list_sessions()

    assert sessions[0].host is None
    assert sessions[1].host is None


async def test_list_sessions_returns_empty_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_proc(monkeypatch, FakeProcess(returncode=1, stderr=b"boom"))
    assert await cs_client.list_sessions() == []


async def test_get_session_parses_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "session_key": "b" * 32,
        "tmux_session_name": "claude-xyz",
        "claude_start_dir": "/home/u/proj",
        "status": None,
        "session_id": None,
        "claude_project_dir": "/home/u/proj",
        "transcript_path": "/t/path.jsonl",
    }
    _patch_proc(monkeypatch, FakeProcess(stdout=json.dumps(payload).encode()))

    detail = await cs_client.get_session("b" * 32)
    assert detail.status is None
    assert detail.transcript_path == "/t/path.jsonl"
    assert detail.project == "proj"


async def test_get_session_reads_agent_session_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # agent-session renamed claude_project_dir -> project_dir, claude_start_dir -> start_dir.
    payload = {
        "session_key": "b" * 32,
        "agent": "claude",
        "tmux_session_name": "cc-xyz",
        "start_dir": "/home/u/proj",
        "status": "idle",
        "session_id": "sid-2",
        "project_dir": "/home/u/proj",
        "transcript_path": "/t/path.jsonl",
    }
    _patch_proc(monkeypatch, FakeProcess(stdout=json.dumps(payload).encode()))

    detail = await cs_client.get_session("b" * 32)
    assert detail.project == "proj"
    assert detail.claude_start_dir == "/home/u/proj"
    assert detail.claude_project_dir == "/home/u/proj"


async def test_get_session_reads_background_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "session_key": "b" * 32,
        "agent": "claude",
        "tmux_session_name": "cc-xyz",
        "start_dir": "/home/u/proj",
        "status": "backgrounded",
        "session_id": "sid-2",
        "project_dir": "/home/u/proj",
        "transcript_path": "/t/path.jsonl",
        "background_tasks": 3,
        "session_crons": 0,
    }
    _patch_proc(monkeypatch, FakeProcess(stdout=json.dumps(payload).encode()))

    detail = await cs_client.get_session("b" * 32)
    assert detail.status == "backgrounded"
    assert detail.background_tasks == 3
    assert detail.session_crons == 0


async def test_get_session_reads_remote_host(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "session_key": "b" * 32,
        "agent": "claude",
        "tmux_session_name": "cc-xyz",
        "start_dir": "/home/martin/Development/krasserm/dashboard",
        "host": "192.168.94.50",
        "status": "idle",
        "session_id": "sid-2",
        "project_dir": "/home/martin/Development/krasserm/dashboard",
        "transcript_path": "/t/path.jsonl",
    }
    _patch_proc(monkeypatch, FakeProcess(stdout=json.dumps(payload).encode()))

    detail = await cs_client.get_session("b" * 32)
    assert detail.host == "192.168.94.50"


async def test_get_session_raises_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_proc(monkeypatch, FakeProcess(returncode=1, stderr=b"no session"))
    with pytest.raises(CsError):
        await cs_client.get_session("deadbeef")


async def test_get_messages_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [{"message": "hi", "timestamp": "2026-06-08T00:00:00"}]
    calls = _patch_proc(monkeypatch, FakeProcess(stdout=json.dumps(payload).encode()))

    msgs = await cs_client.get_messages("c" * 32, last=3)
    assert msgs[0].message == "hi"
    assert calls[0][1:] == ["messages", "c" * 32, "--last", "3", "--json"]


async def test_stop_and_send(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_proc(monkeypatch, FakeProcess())
    await cs_client.stop_session("d" * 32)
    await cs_client.send_message("d" * 32, "hello")
    assert calls[0][1:] == ["stop", "d" * 32]
    assert calls[1][1:] == ["send", "d" * 32, "hello"]


async def test_timeout_kills_and_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProcess(hang=True)
    _patch_proc(monkeypatch, proc)
    with pytest.raises(CsError):
        await cs_client.get_session("e" * 32, timeout=0.05)
    assert proc.killed is True
