"""E2E tests for the asn CLI with real agent sessions."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys

import pytest

from agent_session._daemon import PID_PATH

from .helpers import E2E_BASE

pytestmark = pytest.mark.e2e


def _cs(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    """Run a asn CLI command and return the result."""
    return subprocess.run(
        [sys.executable, "-m", "agent_session.cli", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _start(agent_name: str, project_dir, timeout: float = 60) -> tuple[subprocess.CompletedProcess, str]:
    """Start a session for the given agent and return (result, session_key)."""
    result = _cs(
        "start", str(project_dir), "--agent", agent_name, "--timeout", "40",
        timeout=timeout,
    )
    assert result.returncode == 0, result.stderr
    assert "session" in result.stdout
    key = result.stdout.split("session ")[1].split()[0].strip()
    return result, key


@pytest.fixture(autouse=True)
def set_dev_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_SESSION_DEV_ROOT", str(E2E_BASE.parent))


@pytest.fixture(autouse=True)
async def cleanup_daemon():
    """Ensure daemon is stopped after each test."""
    yield
    # Try to stop daemon gracefully.
    _cs("daemon", "stop", timeout=10)
    # Also kill by PID if it's still around.
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass


class TestCliLifecycle:
    def test_start_and_list(self, agent_name, e2e_project_dir) -> None:
        """asn start should return a session key, asn list should show it."""
        _, key = _start(agent_name, e2e_project_dir)
        assert len(key) == 32

        # List should show the session (short key) and the agent.
        result = _cs("list")
        assert result.returncode == 0
        assert key[:8] in result.stdout
        assert agent_name in result.stdout
        assert "idle" in result.stdout

        _cs("stop", key)

    def test_start_and_status(self, agent_name, e2e_project_dir) -> None:
        """asn status should show session details."""
        _, key = _start(agent_name, e2e_project_dir)

        result = _cs("status", key)
        assert result.returncode == 0
        assert f"Session:     {key}" in result.stdout
        assert "Status:      idle" in result.stdout
        assert f"Agent:       {agent_name}" in result.stdout
        assert "Tmux:" in result.stdout

        _cs("stop", key)

    def test_status_json(self, agent_name, e2e_project_dir) -> None:
        """asn status --json should return valid JSON."""
        _, key = _start(agent_name, e2e_project_dir)

        result = _cs("status", key, "--json")
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["session_key"] == key
        assert data["status"] == "idle"
        assert data["agent"] == agent_name

        _cs("stop", key)

    def test_list_json(self, agent_name, e2e_project_dir) -> None:
        """asn list --json should return valid JSON."""
        _, key = _start(agent_name, e2e_project_dir)

        result = _cs("list", "--json")
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert len(data) == 1
        assert data[0]["session_key"] == key
        assert data[0]["agent"] == agent_name

        _cs("stop", key)

    def test_stop(self, agent_name, e2e_project_dir) -> None:
        """asn stop should remove the session."""
        _, key = _start(agent_name, e2e_project_dir)

        result = _cs("stop", key)
        assert result.returncode == 0

        result = _cs("list")
        assert key not in result.stdout

    def test_stop_all(self, agent_name, e2e_project_dir) -> None:
        """asn stop --all should remove all sessions."""
        _start(agent_name, e2e_project_dir)

        result = _cs("stop", "--all")
        assert result.returncode == 0

    def test_stop_with_prefix(self, agent_name, e2e_project_dir) -> None:
        """asn stop with a key prefix should work."""
        _, key = _start(agent_name, e2e_project_dir)

        result = _cs("stop", key[:4])
        assert result.returncode == 0


class TestCliSendCapture:
    def test_send_message(self, agent_name, e2e_project_dir) -> None:
        """asn send should deliver a message to the agent."""
        _, key = _start(agent_name, e2e_project_dir)

        result = _cs("send", key, "Say exactly 'cli-test-ok'. Nothing else.")
        assert result.returncode == 0

        # Wait for processing.
        import time
        time.sleep(15)

        result = _cs("capture", key)
        assert result.returncode == 0
        assert isinstance(result.stdout, str)

        _cs("stop", key)

    def test_capture(self, agent_name, e2e_project_dir) -> None:
        """asn capture should return tmux pane content."""
        _, key = _start(agent_name, e2e_project_dir)

        result = _cs("capture", key)
        assert result.returncode == 0
        assert len(result.stdout) > 0

        _cs("stop", key)


class TestCliDaemon:
    def test_daemon_status_when_not_running(self) -> None:
        """asn daemon status should report not running when daemon is down."""
        result = _cs("daemon", "status")
        assert result.returncode == 0
        assert "not running" in result.stdout

    def test_daemon_auto_start(self, agent_name, e2e_project_dir) -> None:
        """Daemon should auto-start on first asn start."""
        result = _cs("daemon", "status")
        assert "not running" in result.stdout

        _start(agent_name, e2e_project_dir)

        result = _cs("daemon", "status")
        assert "running" in result.stdout
        assert "pid=" in result.stdout

        _cs("stop", "--all")

    def test_daemon_stop(self, agent_name, e2e_project_dir) -> None:
        """asn daemon stop should shut down the daemon."""
        _start(agent_name, e2e_project_dir)

        result = _cs("daemon", "stop")
        assert result.returncode == 0

        result = _cs("daemon", "status")
        assert "not running" in result.stdout


class TestCliMessages:
    def test_messages_json(self, agent_name, e2e_project_dir) -> None:
        """asn messages --json should return valid JSON."""
        _, key = _start(agent_name, e2e_project_dir)

        result = _cs("messages", key, "--json")
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)

        _cs("stop", key)

    def test_messages_with_last(self, agent_name, e2e_project_dir) -> None:
        """asn messages --last N should return up to N messages."""
        _, key = _start(agent_name, e2e_project_dir)

        result = _cs("messages", key, "--last", "5", "--json")
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) <= 5

        _cs("stop", key)


class TestCliErrors:
    def test_stop_nonexistent(self, agent_name, e2e_project_dir) -> None:
        """asn stop with a bad key should report an error."""
        _, key = _start(agent_name, e2e_project_dir)

        result = _cs("stop", "nonexistent")
        assert result.returncode == 1
        assert "Error" in result.stderr

        _cs("stop", key)

    def test_status_nonexistent(self, agent_name, e2e_project_dir) -> None:
        """asn status with a bad key should report an error."""
        _, key = _start(agent_name, e2e_project_dir)

        result = _cs("status", "nonexistent")
        assert result.returncode == 1
        assert "Error" in result.stderr

        _cs("stop", key)
