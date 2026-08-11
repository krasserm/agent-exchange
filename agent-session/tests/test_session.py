"""Integration tests for AgentSession using fake events (no real agent CLI)."""

import asyncio
import subprocess
import uuid
from pathlib import Path

import libtmux
import pytest

from agent_session import _naming
from agent_session._models import AgentStatus
from agent_session._session import AgentSession
from agent_session.agents import LaunchSpec, get_agent
from conftest import make_event_line

_DETECT_DELAY = 1.0


def append_event(session: AgentSession, event_name: str, session_id: str = "test-session-id", **extra: str) -> None:
    path = session.session_dir / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(make_event_line(event_name, session_id=session_id, **extra) + "\n")


@pytest.fixture
def dev_root(tmp_path: Path) -> Path:
    root = tmp_path / "Development"
    root.mkdir()
    return root


@pytest.fixture
def project_name() -> str:
    """Unique project name per test to avoid tmux session name collisions."""
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def start_dir(dev_root: Path, project_name: str) -> Path:
    d = dev_root / project_name
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def set_dev_root_env(dev_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_SESSION_DEV_ROOT", str(dev_root))
    monkeypatch.setattr("agent_session._naming.SESSIONS_DIR", tmp_path / "sessions")


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


def make_session(start_dir: Path, **spec_kwargs: object) -> AgentSession:
    return AgentSession(
        get_agent("claude"),
        LaunchSpec(start_dir=start_dir, skip_permissions=False, **spec_kwargs),
    )


async def start_with_fake_event(
    session: AgentSession,
    session_id: str = "s1",
    timeout: float = 10,
) -> None:
    """Start an AgentSession and emit a fake SessionStart event."""
    async def emit_start() -> None:
        await asyncio.sleep(_DETECT_DELAY)
        append_event(session, "SessionStart", session_id=session_id)

    asyncio.create_task(emit_start())
    await session.start(timeout=timeout)


class TestStart:
    async def test_creates_tmux_session(
        self, start_dir: Path, tmux_server: libtmux.Server,
    ) -> None:
        session = make_session(start_dir)
        await start_with_fake_event(session)

        try:
            name = await session.get_tmux_session_name()
            assert name is not None
            assert tmux_server.has_session(name)
        finally:
            await session.stop()

    async def test_launch_script_unsets_virtual_env(self, start_dir: Path) -> None:
        # The tmux server's global env may carry VIRTUAL_ENV from whichever
        # process first started it; launch.sh must drop it so agents running
        # `uv` in other projects don't hit the mismatch warning.
        session = make_session(start_dir)
        await start_with_fake_event(session)

        try:
            script = (session.session_dir / "launch.sh").read_text()
            assert "unset VIRTUAL_ENV VIRTUAL_ENV_PROMPT\n" in script
        finally:
            await session.stop()

    async def test_launch_script_quotes_the_state_root(
        self, start_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ASN_HOME is a filesystem path, so it must survive word splitting.

        Unlike AGENT_SESSION_KEY next to it (uuid hex), the state root can carry
        a space -- from $HOME or from ASN_HOME itself. Unquoted, bash sets
        ASN_HOME to the first word and tries to execute the rest, so the hook
        writes its events somewhere nobody watches and the launch breaks with no
        clear cause.
        """
        root = Path("/tmp/asn state root/with space")
        monkeypatch.setattr(_naming, "DAEMON_DIR", root)
        session = make_session(start_dir)
        await start_with_fake_event(session)

        try:
            script_path = session.session_dir / "launch.sh"
            script = script_path.read_text()
            # The whole script must still parse ...
            assert subprocess.run(
                ["bash", "-n", str(script_path)], capture_output=True
            ).returncode == 0, "launch.sh no longer parses"
            # ... and the value must arrive as one word.
            line = next(
                ln for ln in script.splitlines() if ln.startswith("export ASN_HOME=")
            )
            got = subprocess.run(
                ["bash", "-c", f'{line}; printf %s "$ASN_HOME"'],
                capture_output=True, text=True,
            )
            assert got.stdout == str(root), f"ASN_HOME mangled to {got.stdout!r}"
        finally:
            await session.stop()

    async def test_timeout_raises_and_cleans_up(
        self, start_dir: Path, tmux_server: libtmux.Server, session_name_prefix: str,
    ) -> None:
        session = make_session(start_dir)

        with pytest.raises(TimeoutError, match="No SessionStart"):
            await session.start(timeout=2.0)

        # Verify no sessions with our prefix remain.
        for s in tmux_server.sessions:
            assert not s.session_name.startswith(session_name_prefix)

    async def test_creates_session_dir(self, start_dir: Path) -> None:
        session = make_session(start_dir)
        await start_with_fake_event(session)

        try:
            assert session.session_dir.exists()
            assert session.session_dir.parent.name == "sessions"
            assert (session.session_dir / "events.jsonl").exists()
        finally:
            await session.stop()


class TestStop:
    async def test_stop_kills_session(
        self, start_dir: Path, tmux_server: libtmux.Server,
    ) -> None:
        session = make_session(start_dir)
        await start_with_fake_event(session)

        name = await session.get_tmux_session_name()
        assert name is not None
        await session.stop()
        assert not tmux_server.has_session(name)

    async def test_stop_tolerates_already_killed(
        self, start_dir: Path, tmux_server: libtmux.Server,
    ) -> None:
        session = make_session(start_dir)
        await start_with_fake_event(session)

        # Kill externally.
        name = await session.get_tmux_session_name()
        s = tmux_server.sessions.get(session_name=name, default=None)
        assert s is not None
        s.kill()

        await session.stop()

    async def test_double_stop(self, start_dir: Path) -> None:
        session = make_session(start_dir)
        await start_with_fake_event(session)

        await session.stop()
        await session.stop()  # Should not raise.


class TestGetAgentInfo:
    async def test_active_session(self, start_dir: Path) -> None:
        session = make_session(start_dir)
        await start_with_fake_event(session)

        try:
            info = await session.get_agent_info()
            assert info is not None
            assert info.session_id == "s1"
            assert info.status == AgentStatus.IDLE
            assert info.transcript_path.name == "s1.jsonl"
        finally:
            await session.stop()

    async def test_after_session_end(self, start_dir: Path) -> None:
        session = make_session(start_dir)
        await start_with_fake_event(session)

        try:
            append_event(session, "SessionEnd", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)

            assert await session.get_agent_info() is None
        finally:
            await session.stop()

    async def test_status_transitions(self, start_dir: Path) -> None:
        session = make_session(start_dir)
        await start_with_fake_event(session)

        try:
            assert (await session.get_agent_info()).status == AgentStatus.IDLE

            append_event(session, "UserPromptSubmit", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            assert (await session.get_agent_info()).status == AgentStatus.WORKING

            append_event(session, "Stop", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            assert (await session.get_agent_info()).status == AgentStatus.IDLE
        finally:
            await session.stop()

    async def test_session_id_changes(self, start_dir: Path) -> None:
        session = make_session(start_dir)
        await start_with_fake_event(session)

        try:
            assert (await session.get_agent_info()).session_id == "s1"

            append_event(session, "SessionEnd", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            append_event(session, "SessionStart", session_id="s2")
            await asyncio.sleep(_DETECT_DELAY)

            info = await session.get_agent_info()
            assert info is not None
            assert info.session_id == "s2"
        finally:
            await session.stop()

    async def test_before_start(self, start_dir: Path) -> None:
        session = make_session(start_dir)
        assert await session.get_agent_info() is None


class TestCapture:
    async def test_returns_pane_content(self, start_dir: Path) -> None:
        session = make_session(start_dir)
        await start_with_fake_event(session)

        try:
            output = await session.capture()
            assert isinstance(output, str)
        finally:
            await session.stop()

    async def test_requires_active_session(self, start_dir: Path) -> None:
        session = make_session(start_dir)
        with pytest.raises(RuntimeError, match="No active agent session"):
            await session.capture()


class TestSendUserMessage:
    async def test_raises_no_active_session(self, start_dir: Path) -> None:
        session = make_session(start_dir)
        with pytest.raises(RuntimeError, match="No active agent session"):
            await session.send_user_message("hello")

    async def test_raises_after_session_end(self, start_dir: Path) -> None:
        session = make_session(start_dir)
        await start_with_fake_event(session)

        try:
            append_event(session, "SessionEnd", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)

            with pytest.raises(RuntimeError):
                await session.send_user_message("hello")
        finally:
            await session.stop()


class TestProperties:
    async def test_tmux_session_name_after_start(
        self, start_dir: Path, session_name_prefix: str,
    ) -> None:
        session = make_session(start_dir)
        await start_with_fake_event(session)

        try:
            name = await session.get_tmux_session_name()
            assert name is not None
            assert name.startswith(session_name_prefix)
            # Name should include the session key suffix.
            assert name != session_name_prefix
        finally:
            await session.stop()

    async def test_tmux_session_name_after_external_kill(
        self, start_dir: Path, tmux_server: libtmux.Server,
    ) -> None:
        session = make_session(start_dir)
        await start_with_fake_event(session)

        try:
            name = await session.get_tmux_session_name()
            s = tmux_server.sessions.get(session_name=name, default=None)
            assert s is not None
            s.kill()

            assert await session.get_tmux_session_name() is None
        finally:
            await session.stop()

    def test_project_dir_no_worktree(self, start_dir: Path) -> None:
        session = make_session(start_dir)
        assert session.project_dir == start_dir.resolve()

    def test_project_dir_with_worktree(self, start_dir: Path) -> None:
        # start_dir is in no repo, so the base stays start_dir.
        session = make_session(start_dir, worktree="my-branch")
        expected = start_dir.resolve() / ".claude" / "worktrees" / "my-branch"
        assert session.project_dir == expected

    def test_project_dir_with_worktree_in_repo_subdir(self, start_dir: Path) -> None:
        """A subdirectory project resolves its worktree from the repo root (#1).

        The recorded project_dir is existence-checked by get_agent_info(), so
        anchoring it on start_dir made a live session read as ended.
        """
        (start_dir / ".git").mkdir()
        proj = start_dir / "sub" / "proj"
        proj.mkdir(parents=True)
        session = make_session(proj, worktree="my-branch")
        expected = start_dir.resolve() / ".claude" / "worktrees" / "my-branch"
        assert session.project_dir == expected

    def test_session_key_is_unique(self, start_dir: Path) -> None:
        s1 = make_session(start_dir)
        s2 = make_session(start_dir)
        assert s1.session_key != s2.session_key

    def test_session_dir_under_sessions(self, start_dir: Path) -> None:
        session = make_session(start_dir)
        assert session.session_dir.parent.name == "sessions"
        assert session.session_dir.name == session.session_key

    async def test_transcript_path(self, start_dir: Path) -> None:
        session = make_session(start_dir)
        await start_with_fake_event(session)

        try:
            info = await session.get_agent_info()
            assert info is not None
            assert info.transcript_path.name == "s1.jsonl"
            assert "projects" in str(info.transcript_path)
        finally:
            await session.stop()


class TestGetAssistantMessages:
    async def test_returns_empty_before_any_stop(self, start_dir: Path) -> None:
        session = make_session(start_dir)
        await start_with_fake_event(session)
        try:
            msgs = await session.get_assistant_messages()
            assert msgs == []
        finally:
            await session.stop()

    async def test_returns_message_from_stop_event(self, start_dir: Path) -> None:
        session = make_session(start_dir)
        await start_with_fake_event(session)
        try:
            append_event(
                session, "Stop", session_id="s1",
                assistant_message="Hello!",
            )
            await asyncio.sleep(_DETECT_DELAY)

            msgs = await session.get_assistant_messages()
            assert len(msgs) == 1
            assert msgs[0].message == "Hello!"
        finally:
            await session.stop()

    async def test_returns_empty_when_no_monitor(self, start_dir: Path) -> None:
        session = make_session(start_dir)
        msgs = await session.get_assistant_messages()
        assert msgs == []


class TestConcurrentSendUserMessage:
    async def test_lock_serializes_sends(self, start_dir: Path) -> None:
        """Two concurrent send_user_message calls should not interleave."""
        session = make_session(start_dir)
        await start_with_fake_event(session)
        try:
            order: list[tuple[str, ...]] = []

            original_send_text = session._tmux.send_text

            def tracking_send_text(name: str, text: str) -> None:
                order.append(("text", text))
                return original_send_text(name, text)

            session._tmux.send_text = tracking_send_text

            original_send_enter = session._tmux.send_enter

            def tracking_send_enter(name: str) -> None:
                order.append(("enter",))
                result = original_send_enter(name)
                # Stand in for the agent's submit hook: send_user_message now
                # waits for the UserPromptSubmit ack before returning.
                append_event(session, "UserPromptSubmit", session_id="s1")
                return result

            session._tmux.send_enter = tracking_send_enter

            await asyncio.gather(
                session.send_user_message("first"),
                session.send_user_message("second"),
            )

            # With proper serialization: text, enter, text, enter
            # (not text, text, enter, enter).
            assert len(order) == 4
            assert order[0][0] == "text"
            assert order[1][0] == "enter"
            assert order[2][0] == "text"
            assert order[3][0] == "enter"
        finally:
            await session.stop()


class TestCodexReadinessScrollback:
    """codex 0.142.5 renders INLINE (alternate_on=0), so text from a *dismissed*
    startup trust dialog lingers in the tmux normal-buffer SCROLLBACK. Readiness
    detection must reason about the LIVE visible frame only -- otherwise the
    modal-guard in ``CodexAgent.ready_from_pane`` keeps matching the stale
    scrollback text and the session never reports ready, timing out ``asn start``
    even though the composer chevron is on screen.
    """

    def _make_codex_session(self, start_dir: Path) -> AgentSession:
        return AgentSession(
            get_agent("codex"),
            LaunchSpec(start_dir=start_dir, skip_permissions=False),
        )

    def _spawn_pane(self, tmux_server: libtmux.Server, name: str) -> None:
        # Run a controlled process (no shell prompt/echo noise) that: prints the
        # trust-dialog text, scrolls it off the top with padding lines (into
        # scrollback), then leaves only the composer chevron on the visible
        # frame, then sleeps to keep the pane alive. A small 80x10 pane makes the
        # visible/scrollback split deterministic regardless of the host terminal.
        cmd = (
            "printf 'Do you trust this folder?\\n"
            "Press enter to continue to use Codex\\n'; "
            "for i in $(seq 1 30); do echo pad$i; done; "
            "printf '\\342\\200\\272 ask Codex to do something\\n'; "
            "sleep 300"
        )
        tmux_server.cmd("new-session", "-d", "-s", name, "-x", "80", "-y", "10", cmd)

    async def test_ready_detected_from_live_frame_not_scrollback(
        self, start_dir: Path, tmux_server: libtmux.Server,
    ) -> None:
        session = self._make_codex_session(start_dir)
        name = session._session_name
        self._spawn_pane(tmux_server, name)
        try:
            # Let the controlled process render.
            await asyncio.sleep(0.6)

            # Precondition: the dismissed modal text really is in scrollback (so
            # a scrollback-inclusive capture would trip the guard), while the
            # live frame shows the chevron and none of the modal text.
            scrollback = session._tmux.capture_pane(name, 80)
            assert "Do you trust" in scrollback
            visible = session._tmux.capture_visible(name)
            assert "Do you trust" not in visible
            assert "Press enter to continue" not in visible
            assert "›" in visible

            # Readiness must be detected promptly from the live frame. Before the
            # fix this polls a scrollback-inclusive capture, keeps matching the
            # stale modal text, and raises TimeoutError.
            await asyncio.wait_for(session._wait_for_marker(timeout=3.0), timeout=5.0)
        finally:
            s = tmux_server.sessions.get(session_name=name, default=None)
            if s is not None:
                s.kill()

    async def test_not_ready_while_trust_modal_is_live(
        self, start_dir: Path, tmux_server: libtmux.Server,
    ) -> None:
        # The genuine "trust modal still up" case must NOT be treated as ready:
        # here the modal text is on the visible frame (with the selected-item
        # chevron), so readiness must not fire.
        session = self._make_codex_session(start_dir)
        name = session._session_name
        cmd = (
            "printf '\\342\\200\\272 Do you trust this folder?\\n"
            "Press enter to continue to use Codex\\n'; sleep 300"
        )
        tmux_server.cmd("new-session", "-d", "-s", name, "-x", "80", "-y", "10", cmd)
        try:
            await asyncio.sleep(0.4)
            visible = session._tmux.capture_visible(name)
            assert "Do you trust" in visible
            assert not session._agent.ready_from_pane(visible)
        finally:
            s = tmux_server.sessions.get(session_name=name, default=None)
            if s is not None:
                s.kill()


class _FakeTmux:
    """Records send_text/send_enter without touching a real tmux server."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def session_exists(self, name: str) -> bool:
        return True

    def send_text(self, name: str, text: str) -> None:
        self.calls.append(("text", text))

    def send_enter(self, name: str) -> None:
        self.calls.append(("enter",))


class _FakeMonitor:
    """Monitor stub whose wait_for_event follows a scripted outcome list."""

    def __init__(self, outcomes: list[bool]) -> None:
        self.outcomes = list(outcomes)
        self.waits = 0
        self.ended = False

    async def wait_for_event(self, event_name: str, timeout: float):
        self.waits += 1
        assert event_name == "UserPromptSubmit"
        if self.outcomes and self.outcomes.pop(0):
            return object()
        raise TimeoutError(f"no {event_name} within {timeout}s")


def _fake_send_session(start_dir: Path, outcomes: list[bool]):
    session = make_session(start_dir)
    session._started = True
    session._tmux = _FakeTmux()
    session._monitor = _FakeMonitor(outcomes)
    return session


class TestSendUserMessageConfirmation:
    """Enter is retried on a missing ack, but no ack is never an error.

    Both agents emit UserPromptSubmit when a message starts a new turn, so the
    ack confirms a submit quickly and skips the retry. But a busy session takes
    the message into its steering queue without emitting UserPromptSubmit, so
    the absence of the ack is not evidence of non-delivery: the send logs and
    returns success, and the retry Enter doubles as the flush for a genuinely
    parked composer (#3).
    """

    async def test_confirmed_send_presses_enter_once(self, start_dir: Path) -> None:
        session = _fake_send_session(start_dir, [True])
        await session.send_user_message("hello")
        assert session._tmux.calls == [("text", "hello"), ("enter",)]
        assert session._monitor.waits == 1

    async def test_retries_enter_once_when_first_ack_times_out(
        self, start_dir: Path
    ) -> None:
        session = _fake_send_session(start_dir, [False, True])
        await session.send_user_message("hello")
        assert session._tmux.calls == [("text", "hello"), ("enter",), ("enter",)]
        assert session._monitor.waits == 2

    async def test_returns_normally_when_never_acknowledged(
        self, start_dir: Path
    ) -> None:
        """No ack is not evidence of non-delivery: a busy session queues the
        message as a steering attachment and never emits UserPromptSubmit, so
        raising here made callers resend and produce duplicates."""
        session = _fake_send_session(start_dir, [False, False])
        await session.send_user_message("hello")
        assert session._tmux.calls == [("text", "hello"), ("enter",), ("enter",)]
        assert session._monitor.waits == 2

    async def test_slash_command_is_not_confirmed(self, start_dir: Path) -> None:
        """Slash commands are TUI commands, not prompts: no hook fires.

        Measured on claude 2.x: `/help` ran (its output rendered in the pane) and
        `/exit` ended the session (SessionEnd), and neither produced a
        UserPromptSubmit. Confirming them would turn every working slash command
        into an 8s stall and then a raise.
        """
        session = _fake_send_session(start_dir, [])
        await session.send_user_message("/exit")
        assert session._tmux.calls == [("text", "/exit"), ("enter",)]
        assert session._monitor.waits == 0
