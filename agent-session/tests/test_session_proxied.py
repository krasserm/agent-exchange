"""Integration tests for proxied (non-local-native) session behavior.

These exercise the daemon-visible state machine of a proxied session -- most
importantly the DISCONNECTED state and reattach -- using a real *local* tmux
session as the stand-in proxy and fake events, without any ssh/docker/mutagen.
"""

import asyncio
import subprocess
from pathlib import Path

import libtmux
import pytest

from agent_session._models import AgentStatus
from agent_session._monitor import EventMonitor
from agent_session import _session as _session_mod
from agent_session._session import AgentSession
from agent_session.agents import LaunchSpec, Transport, get_agent
from conftest import make_event_line

_DETECT_DELAY = 1.0


@pytest.fixture
def tmux_server() -> libtmux.Server:
    return libtmux.Server()


def make_remote_session(events_path: Path, host: str) -> AgentSession:
    session = AgentSession(
        get_agent("claude"),
        LaunchSpec(start_dir=Path("/srv/proj"), transport=Transport(location="remote", host=host)),
    )
    # Redirect the events file to a local temp path (no real mirror here).
    session._events_path = events_path
    return session


def emit(events_path: Path, event_name: str, session_id: str = "s1", **extra: str) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a") as f:
        f.write(make_event_line(event_name, session_id=session_id, **extra) + "\n")


async def fake_start(session: AgentSession, events_path: Path) -> None:
    """Bring a proxied session into the 'started' state with a real local proxy
    tmux and a running monitor, as if _start_proxied had completed -- but
    without ssh/docker."""
    session._monitor = EventMonitor(events_path)
    session._monitor.start()
    await asyncio.to_thread(
        session._tmux.create_session, session.session_name, Path.home()
    )
    session._started = True
    emit(events_path, "SessionStart")
    await asyncio.sleep(_DETECT_DELAY)


class TestDisconnectedState:
    async def test_local_proxy_gone_reports_disconnected_not_ended(
        self, tmp_path: Path, tmux_server: libtmux.Server,
    ) -> None:
        events = tmp_path / "events.jsonl"
        session = make_remote_session(events, host="test-host-1")
        await fake_start(session, events)
        try:
            info = await session.get_agent_info()
            assert info is not None and info.status == AgentStatus.IDLE

            # Simulate the local tmux being killed directly (no asn close).
            tmux_server.kill_session(session.session_name)

            info = await session.get_agent_info()
            assert info is not None, "a disconnected session must remain visible"
            assert info.status == AgentStatus.DISCONNECTED
            # session_id is retained from durable events.
            assert info.session_id == "s1"
        finally:
            await session.stop()

    async def test_real_session_end_is_ended_not_disconnected(
        self, tmp_path: Path,
    ) -> None:
        events = tmp_path / "events.jsonl"
        session = make_remote_session(events, host="test-host-2")
        await fake_start(session, events)
        try:
            emit(events, "SessionEnd")
            await asyncio.sleep(_DETECT_DELAY)
            # A real SessionEnd means ended, even though the proxy still exists.
            assert await session.get_agent_info() is None
        finally:
            await session.stop()


class TestReattach:
    async def test_reattach_recreates_local_proxy(
        self, tmp_path: Path, tmux_server: libtmux.Server, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        events = tmp_path / "events.jsonl"
        session = make_remote_session(events, host="test-host-3")
        await fake_start(session, events)
        try:
            tmux_server.kill_session(session.session_name)
            assert await session.get_tmux_session_name() is None

            # Avoid spawning a real ssh loop: stub the proxy spawn to just make
            # a local tmux session of the right name.
            async def fake_spawn() -> None:
                await asyncio.to_thread(
                    session._tmux.create_session, session.session_name, Path.home()
                )

            monkeypatch.setattr(session, "_spawn_local_proxy", fake_spawn)

            async def fake_wait(timeout: float = 20.0) -> None:
                return None

            monkeypatch.setattr(session, "_wait_proxy_attached", fake_wait)
            await session.reattach()
            assert await session.get_tmux_session_name() is not None
        finally:
            await session.stop()

    async def test_reattach_idempotent_when_present(self, tmp_path: Path) -> None:
        events = tmp_path / "events.jsonl"
        session = make_remote_session(events, host="test-host-4")
        await fake_start(session, events)
        try:
            # Proxy already present -> reattach is a no-op (does not raise).
            await session.reattach()
            assert await session.get_tmux_session_name() is not None
        finally:
            await session.stop()

    async def test_reattach_rejected_for_local_native(self, tmp_path: Path) -> None:
        session = AgentSession(
            get_agent("claude"), LaunchSpec(start_dir=Path.home())
        )
        with pytest.raises(RuntimeError, match="local-native"):
            await session.reattach()


class TestTranscriptPathFallback:
    """The transcript-path fallback only answers for local-native.

    ``Agent.transcript_path`` derives a path from *this* machine's ``$HOME``,
    which is where the transcript lives only when the agent runs here directly.
    A container writes it under its own home and a remote under the agent host's,
    and neither is reachable through a control-host path -- so guessing one
    yields a path that does not exist, and for a remote session it also drags the
    local filesystem's symlinks in: a remote ``/home/martin/x`` came back as
    ``/System/Volumes/Data/home/martin/x`` on macOS, the same rewrite 539cc6c
    removed elsewhere. ``None`` keeps "unknown" honest, which is what
    ``Agent.transcript_path`` already documents as its own no-answer.
    """

    def _session(self, transport: Transport, start_dir: str = "/srv/proj") -> AgentSession:
        return AgentSession(
            get_agent("claude"), LaunchSpec(start_dir=Path(start_dir), transport=transport)
        )

    def test_remote_native_declines_to_guess(self) -> None:
        s = self._session(Transport(location="remote", host="h"))
        assert s._fallback_transcript_path("sid-1") is None

    def test_remote_container_declines_to_guess(self) -> None:
        s = self._session(Transport(location="remote", host="h", runtime="container"))
        assert s._fallback_transcript_path("sid-1") is None

    def test_local_container_declines_to_guess(self) -> None:
        """The container's transcript is under its own home, not the host's."""
        s = self._session(Transport(runtime="container"), start_dir=str(Path.home() / "proj"))
        assert s._fallback_transcript_path("sid-1") is None

    def test_local_native_still_derives_the_path(self) -> None:
        """The one case where the fallback is legitimate must keep working."""
        proj = Path.home() / "proj"
        s = self._session(Transport(), start_dir=str(proj))
        got = s._fallback_transcript_path("sid-1")
        assert got == get_agent("claude").transcript_path(proj.resolve(), "sid-1")
        assert got is not None and got.is_relative_to(Path.home() / ".claude")

    def test_no_local_symlink_rewrite_leaks_into_a_remote_path(
        self, tmp_path: Path
    ) -> None:
        """The concrete regression: a remote path must not be resolved here.

        ``/srv`` need not exist locally; what matters is that nothing built from
        the control host's filesystem or home appears in the answer.
        """
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        s = self._session(Transport(location="remote", host="h"), start_dir=str(link / "proj"))
        assert s._fallback_transcript_path("sid-1") is None


class TestTranscriptPathReported:
    async def test_reported_path_is_preferred_for_a_remote_session(
        self, tmp_path: Path
    ) -> None:
        """An agent-reported transcript path passes through untouched."""
        events = tmp_path / "events.jsonl"
        session = make_remote_session(events, host="test-host-tp")
        session._monitor = EventMonitor(events)
        session._monitor.start()
        await asyncio.to_thread(
            session._tmux.create_session, session.session_name, Path.home()
        )
        session._started = True
        emit(events, "SessionStart", session_id="sid-2",
             transcript_path="/remote/home/.claude/projects/-srv-proj/sid-2.jsonl")
        await asyncio.sleep(_DETECT_DELAY)
        try:
            info = await session.get_agent_info()
            assert info is not None
            assert str(info.transcript_path) == (
                "/remote/home/.claude/projects/-srv-proj/sid-2.jsonl"
            )
        finally:
            await session.stop()


class TestProxyAttachConfirmation:
    """An unconfirmed attach must fail, not be assumed.

    ``list-clients`` going quiet means the ssh/docker-exec/attach chain has not
    connected, and anything sent then lands in a pane nobody is forwarding --
    silently dropped. Returning anyway turned a slow attach into a lost message
    with no diagnostic: this is what made
    ``test_disconnected_then_reattach[remote-container@...]`` fail on both
    remote-container cells while passing everywhere faster.
    """

    def _session(self, tmp_path: Path) -> AgentSession:
        return make_remote_session(tmp_path / "events.jsonl", host="attach-host")

    async def test_raises_when_attach_is_never_confirmed(self, tmp_path: Path) -> None:
        session = self._session(tmp_path)
        probes = []

        def never_attached(command: str, check: bool = True):
            probes.append(command)
            return subprocess.CompletedProcess(args=command, returncode=0, stdout=b"", stderr=b"")

        session._run_local = never_attached  # type: ignore[method-assign]
        with pytest.raises(TimeoutError, match="attach"):
            await session._wait_proxy_attached(timeout=1.0)
        assert probes, "gave up without probing at all"
        assert "list-clients" in probes[0]

    async def test_returns_once_a_client_is_attached(self, tmp_path: Path) -> None:
        session = self._session(tmp_path)

        def attached(command: str, check: bool = True):
            return subprocess.CompletedProcess(args=command, returncode=0, stdout=b"x\n", stderr=b"")

        session._run_local = attached  # type: ignore[method-assign]
        await session._wait_proxy_attached(timeout=5.0)

    async def test_error_names_the_session_and_the_budget(self, tmp_path: Path) -> None:
        """A dropped message was undiagnosable; the failure must point at itself."""
        session = self._session(tmp_path)
        session._run_local = lambda command, check=True: subprocess.CompletedProcess(  # type: ignore[method-assign]
            args=command, returncode=1, stdout=b"", stderr=b"boom"
        )
        with pytest.raises(TimeoutError) as excinfo:
            await session._wait_proxy_attached(timeout=1.0)
        msg = str(excinfo.value)
        assert session.remote_session_name is not None
        assert session.remote_session_name in msg
        assert "1.0" in msg or "1" in msg

    async def test_reattach_propagates_the_failure(
        self, tmp_path: Path, tmux_server: libtmux.Server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """reattach must not report success when the proxy never attached."""
        events = tmp_path / "events.jsonl"
        session = make_remote_session(events, host="attach-host-2")
        await fake_start(session, events)
        try:
            tmux_server.kill_session(session.session_name)

            async def fake_spawn() -> None:
                await asyncio.to_thread(
                    session._tmux.create_session, session.session_name, Path.home()
                )

            monkeypatch.setattr(session, "_spawn_local_proxy", fake_spawn)
            monkeypatch.setattr(
                session, "_run_local",
                lambda command, check=True: subprocess.CompletedProcess(
                    args=command, returncode=0, stdout=b"", stderr=b""
                ),
            )
            monkeypatch.setattr(_session_mod, "PROXY_ATTACH_TIMEOUT", 1.0)
            with pytest.raises(TimeoutError):
                await session.reattach()
        finally:
            await session.stop()
