"""Unit tests for daemon components (no I/O, no tmux)."""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_session._daemon import DaemonServer, SessionRegistry, scrub_environment
from agent_session._models import AgentInfo, AgentStatus
from agent_session.agents import Transport


def _fake_session(key: str, *, info=..., tmux="cx-fake", transport=None):
    """A stand-in AgentSession with async info/tmux accessors.

    ``info=...`` (the default sentinel) yields a healthy idle AgentInfo; pass an
    Exception instance to make ``get_agent_info`` raise, mimicking a session
    whose live state cannot be read this instant (e.g. a transient libtmux error
    under concurrent load).
    """
    s = MagicMock()
    s.session_key = key
    s.agent_name = "codex"
    s.start_dir = Path("/tmp/proj")
    s.project_dir = Path("/tmp/proj")
    s.transport = transport if transport is not None else Transport()
    if isinstance(info, BaseException):
        s.get_agent_info = AsyncMock(side_effect=info)
    else:
        resolved = (
            AgentInfo(session_id="s1", status=AgentStatus.IDLE, transcript_path=None)
            if info is ... else info
        )
        s.get_agent_info = AsyncMock(return_value=resolved)
    s.get_tmux_session_name = AsyncMock(return_value=tmux)
    return s


class TestHandleListResilience:
    """A single unreadable session must not blank out the whole list.

    ``asn list`` fans out ``get_agent_info``/``get_tmux_session_name`` across
    every session. When that fan-out fails fast, one session in a transient bad
    state turns the entire request into an error -- so ``asn list`` intermittently
    reports *no* sessions even while others are perfectly live.
    """

    async def test_one_failing_session_does_not_empty_list(self) -> None:
        server = DaemonServer()
        server._registry.add(_fake_session("aaaa1111"))
        server._registry.add(_fake_session("bbbb2222", info=RuntimeError("boom")))

        result = await server._handle_list()

        keys = {r["session_key"] for r in result}
        assert keys == {"aaaa1111", "bbbb2222"}, (
            "list must still enumerate every registered session"
        )
        healthy = next(r for r in result if r["session_key"] == "aaaa1111")
        assert healthy["status"] == "idle"

    async def test_failing_session_reported_with_unknown_status(self) -> None:
        server = DaemonServer()
        server._registry.add(_fake_session("bbbb2222", info=RuntimeError("boom")))

        result = await server._handle_list()

        assert len(result) == 1
        assert result[0]["session_key"] == "bbbb2222"
        assert result[0]["status"] is None


class TestSessionHost:
    """``list``/``status`` JSON must carry the transport host.

    agent-session-web shows which machine a session actually runs on, and the
    only durable record of that is ``transport.host`` in ``meta.json``. Without
    it in the payload, every remote session looks local to any consumer.
    """

    async def test_list_reports_remote_host(self) -> None:
        server = DaemonServer()
        server._registry.add(_fake_session(
            "aaaa1111",
            transport=Transport(location="remote", host="192.168.94.50"),
        ))

        result = await server._handle_list()

        assert result[0]["host"] == "192.168.94.50"

    async def test_list_reports_null_host_for_local(self) -> None:
        server = DaemonServer()
        server._registry.add(_fake_session("aaaa1111"))

        result = await server._handle_list()

        assert result[0]["host"] is None

    async def test_status_reports_remote_host(self) -> None:
        server = DaemonServer()
        server._registry.add(_fake_session(
            "aaaa1111",
            transport=Transport(location="remote", host="192.168.94.50", runtime="container", container="cc-x"),
        ))

        result = await server._handle_status({"key": "aaaa1111"})

        assert result["host"] == "192.168.94.50"

    async def test_status_reports_null_host_for_local(self) -> None:
        server = DaemonServer()
        server._registry.add(_fake_session("aaaa1111"))

        result = await server._handle_status({"key": "aaaa1111"})

        assert result["host"] is None

    async def test_degraded_list_row_still_carries_host(self) -> None:
        """A session whose live state is unreadable still knows where it runs."""
        server = DaemonServer()
        server._registry.add(_fake_session(
            "bbbb2222",
            info=RuntimeError("boom"),
            transport=Transport(location="remote", host="192.168.94.51"),
        ))

        result = await server._handle_list()

        assert result[0]["status"] is None
        assert result[0]["host"] == "192.168.94.51"


class TestSessionRegistry:
    def test_add_and_get(self) -> None:
        reg = SessionRegistry()
        session = MagicMock()
        session.session_key = "abcd1234"
        reg.add(session)
        assert reg.get("abcd1234") is session

    def test_get_missing_raises(self) -> None:
        reg = SessionRegistry()
        with pytest.raises(KeyError, match="No session with key"):
            reg.get("nonexistent")

    def test_remove(self) -> None:
        reg = SessionRegistry()
        session = MagicMock()
        session.session_key = "abcd1234"
        reg.add(session)
        reg.remove("abcd1234")
        assert reg.is_empty

    def test_remove_missing_is_noop(self) -> None:
        reg = SessionRegistry()
        reg.remove("nonexistent")  # Should not raise.

    def test_resolve_key_exact(self) -> None:
        reg = SessionRegistry()
        session = MagicMock()
        session.session_key = "abcd1234"
        reg.add(session)
        assert reg.resolve_key("abcd1234") == "abcd1234"

    def test_resolve_key_prefix(self) -> None:
        reg = SessionRegistry()
        session = MagicMock()
        session.session_key = "abcd1234"
        reg.add(session)
        assert reg.resolve_key("abc") == "abcd1234"

    def test_resolve_key_single_char(self) -> None:
        reg = SessionRegistry()
        session = MagicMock()
        session.session_key = "abcd1234"
        reg.add(session)
        assert reg.resolve_key("a") == "abcd1234"

    def test_resolve_key_no_match(self) -> None:
        reg = SessionRegistry()
        session = MagicMock()
        session.session_key = "abcd1234"
        reg.add(session)
        with pytest.raises(KeyError, match="No session matching"):
            reg.resolve_key("xyz")

    def test_resolve_key_ambiguous(self) -> None:
        reg = SessionRegistry()
        s1 = MagicMock()
        s1.session_key = "abcd1234"
        s2 = MagicMock()
        s2.session_key = "abce5678"
        reg.add(s1)
        reg.add(s2)
        with pytest.raises(KeyError, match="Ambiguous prefix"):
            reg.resolve_key("abc")

    def test_resolve_key_ambiguous_shows_matches(self) -> None:
        reg = SessionRegistry()
        s1 = MagicMock()
        s1.session_key = "abcd1234"
        s2 = MagicMock()
        s2.session_key = "abce5678"
        reg.add(s1)
        reg.add(s2)
        with pytest.raises(KeyError, match="abcd1234.*abce5678"):
            reg.resolve_key("abc")

    def test_resolve_key_not_ambiguous_with_longer_prefix(self) -> None:
        reg = SessionRegistry()
        s1 = MagicMock()
        s1.session_key = "abcd1234"
        s2 = MagicMock()
        s2.session_key = "abce5678"
        reg.add(s1)
        reg.add(s2)
        assert reg.resolve_key("abcd") == "abcd1234"

    def test_all_keys(self) -> None:
        reg = SessionRegistry()
        s1 = MagicMock()
        s1.session_key = "aaaa1111"
        s2 = MagicMock()
        s2.session_key = "bbbb2222"
        reg.add(s1)
        reg.add(s2)
        assert sorted(reg.all_keys()) == ["aaaa1111", "bbbb2222"]

    def test_is_empty(self) -> None:
        reg = SessionRegistry()
        assert reg.is_empty
        session = MagicMock()
        session.session_key = "abcd1234"
        reg.add(session)
        assert not reg.is_empty


class TestOrphanRecovery:
    async def test_kills_orphaned_tmux_sessions(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        # Create a fake meta.json
        meta_dir = tmp_path / "sessions" / "abcd1234"
        meta_dir.mkdir(parents=True)
        meta = {
            "session_key": "abcd1234",
            "tmux_session_name": "cc-test-abcd1234",
        }
        (meta_dir / "meta.json").write_text(json.dumps(meta))

        mock_tmux = MagicMock()
        mock_tmux.session_exists.return_value = True

        from agent_session._daemon import DaemonServer

        server = DaemonServer()

        with (
            patch("agent_session._naming.SESSIONS_DIR", tmp_path / "sessions"),
            patch("agent_session._tmux.TmuxManager", return_value=mock_tmux),
        ):
            await server._recover_orphans()

        mock_tmux.kill_session.assert_called_once_with("cc-test-abcd1234")
        # meta.json removed but directory may still exist (events preserved).
        assert not (meta_dir / "meta.json").exists()

    async def test_skips_dead_tmux_sessions(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        meta_dir = tmp_path / "sessions" / "abcd1234"
        meta_dir.mkdir(parents=True)
        meta = {
            "session_key": "abcd1234",
            "tmux_session_name": "cc-test-abcd1234",
        }
        (meta_dir / "meta.json").write_text(json.dumps(meta))

        mock_tmux = MagicMock()
        mock_tmux.session_exists.return_value = False

        from agent_session._daemon import DaemonServer

        server = DaemonServer()

        with (
            patch("agent_session._naming.SESSIONS_DIR", tmp_path / "sessions"),
            patch("agent_session._tmux.TmuxManager", return_value=mock_tmux),
        ):
            await server._recover_orphans()

        mock_tmux.kill_session.assert_not_called()
        assert not (meta_dir / "meta.json").exists()

    async def test_no_sessions_dir(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from agent_session._daemon import DaemonServer

        server = DaemonServer()
        nonexistent = tmp_path / "sessions"

        with patch("agent_session._naming.SESSIONS_DIR", nonexistent):
            await server._recover_orphans()  # Should not raise.


class TestSingletonLock:
    """Only one daemon may hold the lock at a time (prevents proliferation)."""

    def test_second_daemon_cannot_acquire(self, tmp_path: Path, monkeypatch) -> None:
        from agent_session._daemon import DaemonServer

        monkeypatch.setattr("agent_session._daemon.LOCK_PATH", tmp_path / "daemon.lock")
        monkeypatch.setattr("agent_session._naming.DAEMON_DIR", tmp_path)

        first = DaemonServer()
        second = DaemonServer()
        try:
            assert first._acquire_singleton_lock() is True
            assert second._acquire_singleton_lock() is False
        finally:
            first._release_singleton_lock()
            second._release_singleton_lock()

    def test_lock_reacquirable_after_release(self, tmp_path: Path, monkeypatch) -> None:
        from agent_session._daemon import DaemonServer

        monkeypatch.setattr("agent_session._daemon.LOCK_PATH", tmp_path / "daemon.lock")
        monkeypatch.setattr("agent_session._naming.DAEMON_DIR", tmp_path)

        first = DaemonServer()
        assert first._acquire_singleton_lock() is True
        first._release_singleton_lock()

        second = DaemonServer()
        try:
            assert second._acquire_singleton_lock() is True
        finally:
            second._release_singleton_lock()


class TestCleanupOwnership:
    """Cleanup must not delete socket/pid owned by another (live) daemon."""

    async def test_cleanup_skips_files_when_not_bound(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from agent_session._daemon import DaemonServer

        sock = tmp_path / "agent-session.sock"
        pid = tmp_path / "daemon.pid"
        sock.write_text("")
        pid.write_text("99999")  # belongs to some other (live) daemon

        monkeypatch.setattr("agent_session._daemon.SOCKET_PATH", sock)
        monkeypatch.setattr("agent_session._daemon.PID_PATH", pid)
        monkeypatch.setattr("agent_session._naming.SESSIONS_DIR", tmp_path / "sessions")

        server = DaemonServer()  # never bound; _bound stays False
        await server._cleanup()

        assert sock.exists(), "non-owner cleanup must not remove the socket"
        assert pid.exists(), "non-owner cleanup must not remove the pid file"

    async def test_cleanup_removes_files_when_bound(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from agent_session._daemon import DaemonServer

        sock = tmp_path / "agent-session.sock"
        pid = tmp_path / "daemon.pid"
        sock.write_text("")
        pid.write_text(str(os.getpid()))

        monkeypatch.setattr("agent_session._daemon.SOCKET_PATH", sock)
        monkeypatch.setattr("agent_session._daemon.PID_PATH", pid)
        monkeypatch.setattr("agent_session._naming.SESSIONS_DIR", tmp_path / "sessions")

        server = DaemonServer()
        server._bound = True
        await server._cleanup()

        assert not sock.exists()
        assert not pid.exists()


class TestProtocol:
    """Test request/response serialization expectations."""

    def test_request_format(self) -> None:
        request = {"method": "start", "params": {"start_dir": "/tmp/test"}}
        serialized = json.dumps(request)
        parsed = json.loads(serialized)
        assert parsed["method"] == "start"
        assert parsed["params"]["start_dir"] == "/tmp/test"

    def test_success_response_format(self) -> None:
        response = {"ok": True, "result": {"session_key": "abcd1234"}}
        serialized = json.dumps(response)
        parsed = json.loads(serialized)
        assert parsed["ok"] is True
        assert parsed["result"]["session_key"] == "abcd1234"

    def test_error_response_format(self) -> None:
        response = {"ok": False, "error": "No session matching 'xyz'"}
        serialized = json.dumps(response)
        parsed = json.loads(serialized)
        assert parsed["ok"] is False
        assert "xyz" in parsed["error"]


class TestScrubEnvironment:
    """The daemon must not leak the venv it was started from into sessions.

    The daemon inherits VIRTUAL_ENV and a venv-first PATH from the shell that
    auto-started it; the tmux server (forked as the daemon's child) seeds its
    global environment from the daemon's, so every session would inherit them.
    """

    def test_removes_venv_vars_and_venv_path_entries(self) -> None:
        own_bin = sys.prefix + "/bin"
        foreign_venv = "/srv/other-project/.venv"
        env = {
            "VIRTUAL_ENV": foreign_venv,
            "VIRTUAL_ENV_PROMPT": "other-project",
            "PATH": f"{foreign_venv}/bin:{own_bin}:/usr/local/bin:/usr/bin",
            "HOME": "/Users/x",
        }

        scrub_environment(env)

        assert "VIRTUAL_ENV" not in env
        assert "VIRTUAL_ENV_PROMPT" not in env
        assert env["PATH"] == "/usr/local/bin:/usr/bin"
        assert env["HOME"] == "/Users/x"  # unrelated vars untouched

    def test_strips_only_exact_path_entries(self) -> None:
        own_bin = sys.prefix + "/bin"
        env = {"PATH": f"{own_bin}:/usr/bin:{own_bin}-extra:/opt/{own_bin.lstrip('/')}"}

        scrub_environment(env)

        # Only the exact entry goes; superstrings survive.
        assert env["PATH"] == f"/usr/bin:{own_bin}-extra:/opt/{own_bin.lstrip('/')}"

    def test_noop_without_venv_or_path(self) -> None:
        env: dict[str, str] = {"HOME": "/Users/x"}
        scrub_environment(env)
        assert env == {"HOME": "/Users/x"}


class TestSelectPrunable:
    """Retention of ended session dirs under the state root (issue #4)."""

    @staticmethod
    def _mk(root: Path, key: str, *, age_days: float, meta: bool) -> Path:
        d = root / key
        d.mkdir(parents=True)
        (d / "events.jsonl").write_text("{}\n")
        if meta:
            (d / "meta.json").write_text("{}")
        stamp = time.time() - age_days * 86400
        os.utime(d, (stamp, stamp))
        return d

    def test_prunes_old_ended_dir(self, tmp_path: Path) -> None:
        from agent_session._daemon import select_prunable

        old = self._mk(tmp_path, "old", age_days=8, meta=False)
        assert select_prunable(tmp_path, live_keys=set()) == [old]

    def test_keeps_recent_ended_dir(self, tmp_path: Path) -> None:
        """events.jsonl is the evidence used to diagnose message-plane bugs."""
        from agent_session._daemon import select_prunable

        self._mk(tmp_path, "recent", age_days=3, meta=False)
        assert select_prunable(tmp_path, live_keys=set()) == []

    def test_keeps_dir_with_meta(self, tmp_path: Path) -> None:
        """A meta.json means a session the daemon has not reaped yet."""
        from agent_session._daemon import select_prunable

        self._mk(tmp_path, "orphan", age_days=30, meta=True)
        assert select_prunable(tmp_path, live_keys=set()) == []

    def test_keeps_live_key_even_without_meta(self, tmp_path: Path) -> None:
        """The dir is created inside start(); meta lands only after it returns.

        So meta-absence alone is not "ended" -- it is also every session that is
        still starting. Deleting one would take its events.jsonl out from under
        the running agent's hooks.
        """
        from agent_session._daemon import select_prunable

        self._mk(tmp_path, "starting", age_days=99, meta=False)
        assert select_prunable(tmp_path, live_keys={"starting"}) == []

    def test_ignores_files_and_missing_root(self, tmp_path: Path) -> None:
        from agent_session._daemon import select_prunable

        (tmp_path / "stray.txt").write_text("x")
        assert select_prunable(tmp_path, live_keys=set()) == []
        assert select_prunable(tmp_path / "nope", live_keys=set()) == []


class TestDeferredMaintenancePrunes:
    async def test_prunes_after_reaping(self, tmp_path: Path, monkeypatch) -> None:
        """The deferred task prunes as well as reaps (issue #4)."""
        sessions = tmp_path / "sessions"
        stale = sessions / "stale"
        stale.mkdir(parents=True)
        (stale / "events.jsonl").write_text("{}\n")
        old = time.time() - 30 * 86400
        os.utime(stale, (old, old))

        fresh = sessions / "fresh"
        fresh.mkdir()

        monkeypatch.setattr("agent_session._naming.SESSIONS_DIR", sessions)

        server = DaemonServer()
        await server._run_deferred_maintenance([])

        assert not stale.exists()
        assert fresh.exists()
