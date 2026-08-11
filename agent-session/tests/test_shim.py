"""Unit tests for the bundled ``asn`` message-plane shim (bash + jq).

Drives the real shell script: ``send`` must write a well-formed outbox line,
and ``messages``/``status``/``capture`` must speak the read-endpoint envelope
against a stub TCP server and print the result.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

SHIM = Path(__import__("agent_session").__file__).parent / "plugin" / "asn"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="shim requires jq"
)


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SHIM), *args],
        env=env, capture_output=True, text=True, timeout=15,
    )


class TestSend:
    def test_writes_well_formed_outbox_line(self, tmp_path: Path) -> None:
        env = {"HOME": str(tmp_path), "AGENT_SESSION_KEY": "selfkey0000", "PATH": _path()}
        r = _run(["send", "peerkey1111", "hello world"], env)
        assert r.returncode == 0, r.stderr

        outbox = tmp_path / ".agent-session" / "sessions" / "selfkey0000" / "outbox.jsonl"
        lines = outbox.read_text().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["from"] == "selfkey0000"
        assert obj["to"] == "peerkey1111"
        assert obj["message"] == "hello world"
        assert obj["ts"]

    def test_appends_multiple_lines(self, tmp_path: Path) -> None:
        env = {"HOME": str(tmp_path), "AGENT_SESSION_KEY": "selfkey0000", "PATH": _path()}
        _run(["send", "k", "one"], env)
        _run(["send", "k", "two"], env)
        outbox = tmp_path / ".agent-session" / "sessions" / "selfkey0000" / "outbox.jsonl"
        msgs = [json.loads(l)["message"] for l in outbox.read_text().splitlines()]
        assert msgs == ["one", "two"]


class _StubEndpoint:
    """One-shot TCP server speaking the read-endpoint line-JSON envelope."""

    def __init__(self, response: dict) -> None:
        self._response = response
        self.requests: list[dict] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "_StubEndpoint":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            if data:
                self.requests.append(json.loads(data.decode().splitlines()[0]))
            conn.sendall((json.dumps(self._response) + "\n").encode())


def _path() -> str:
    import os
    return os.environ.get("PATH", "/usr/bin:/bin")


class TestReads:
    def test_messages_prints_result_and_forwards_token(self) -> None:
        resp = {"ok": True, "result": {"messages": [{"message": "pong", "timestamp": "t"}]}}
        with _StubEndpoint(resp) as srv:
            env = {
                "HOME": "/tmp", "PATH": _path(),
                "ASN_READ_ADDR": f"127.0.0.1:{srv.port}",
                "ASN_READ_TOKEN": "secret-token",
            }
            r = _run(["messages", "peerkey1111"], env)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "pong"
        assert srv.requests[0]["token"] == "secret-token"
        assert srv.requests[0]["method"] == "messages"
        assert srv.requests[0]["params"] == {"key": "peerkey1111", "last": 1}

    def test_messages_last_flag(self) -> None:
        resp = {"ok": True, "result": {"messages": []}}
        with _StubEndpoint(resp) as srv:
            env = {
                "HOME": "/tmp", "PATH": _path(),
                "ASN_READ_ADDR": f"127.0.0.1:{srv.port}",
                "ASN_READ_TOKEN": "t",
            }
            _run(["messages", "k", "--last", "3"], env)
        assert srv.requests[0]["params"] == {"key": "k", "last": 3}

    def test_status_prints_status_value(self) -> None:
        resp = {"ok": True, "result": {"status": "idle", "session_key": "k"}}
        with _StubEndpoint(resp) as srv:
            env = {
                "HOME": "/tmp", "PATH": _path(),
                "ASN_READ_ADDR": f"127.0.0.1:{srv.port}",
                "ASN_READ_TOKEN": "t",
            }
            r = _run(["status", "k"], env)
        assert r.stdout.strip() == "idle"
        assert srv.requests[0]["method"] == "status"

    def test_capture_prints_content(self) -> None:
        resp = {"ok": True, "result": {"content": "TUI TEXT HERE"}}
        with _StubEndpoint(resp) as srv:
            env = {
                "HOME": "/tmp", "PATH": _path(),
                "ASN_READ_ADDR": f"127.0.0.1:{srv.port}",
                "ASN_READ_TOKEN": "t",
            }
            r = _run(["capture", "k"], env)
        assert r.stdout.strip() == "TUI TEXT HERE"

    def test_error_response_goes_to_stderr_nonzero(self) -> None:
        resp = {"ok": False, "error": "invalid or missing token"}
        with _StubEndpoint(resp) as srv:
            env = {
                "HOME": "/tmp", "PATH": _path(),
                "ASN_READ_ADDR": f"127.0.0.1:{srv.port}",
                "ASN_READ_TOKEN": "t",
            }
            r = _run(["messages", "k"], env)
        assert r.returncode != 0
        assert "invalid or missing token" in r.stderr
