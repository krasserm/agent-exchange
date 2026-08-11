"""E2E tests: session lifecycle with real agents."""

import pytest

from agent_session import AgentSession, AgentStatus

from .helpers import (
    active_info,
    capture_pane,
    read_events,
    wait_for_idle,
    wait_for_status,
)

pytestmark = pytest.mark.e2e


class TestLifecycle:
    async def test_start_and_idle(self, agent_session: AgentSession) -> None:
        """After start(), session should be ready and IDLE."""
        info = await agent_session.get_agent_info()
        assert info is not None
        assert info.status == AgentStatus.IDLE
        assert await agent_session.get_tmux_session_name() is not None

        # Some agents (codex) assign a session id only on the first turn.
        info = await active_info(agent_session)
        assert info.session_id is not None
        assert len(info.session_id) > 0

    async def test_send_user_message_and_status_transitions(
        self, agent_session: AgentSession,
    ) -> None:
        """Send a prompt, observe WORKING -> IDLE, verify side effect."""
        await agent_session.send_user_message(
            "Create a file called hello.txt containing exactly 'hello world'. "
            "Do not output anything else."
        )

        await wait_for_status(agent_session, AgentStatus.WORKING, timeout=15)
        await wait_for_idle(agent_session, timeout=90)

        hello = agent_session.project_dir / "hello.txt"
        assert hello.exists()
        assert "hello world" in hello.read_text()

        output = capture_pane(await agent_session.get_tmux_session_name())
        assert output

    async def test_stop_kills_session(self, make_session, e2e_project_dir) -> None:
        """After stop(), tmux session should be gone."""
        session = make_session(e2e_project_dir)
        await session.start(timeout=45)
        name = await session.get_tmux_session_name()
        assert name is not None

        await session.stop()
        assert await session.get_tmux_session_name() is None

    async def test_transcript_path_exists(
        self, agent_session: AgentSession,
    ) -> None:
        """After a prompt, the transcript file should exist."""
        await agent_session.send_user_message(
            "Say exactly: 'test response'. Nothing else."
        )
        await wait_for_idle(agent_session, timeout=90)

        info = await agent_session.get_agent_info()
        assert info is not None
        assert info.transcript_path is not None
        assert info.transcript_path.exists()

    async def test_session_id_stable(
        self, agent_session: AgentSession,
    ) -> None:
        """Session ID should remain the same across multiple prompts."""
        first_id = (await active_info(agent_session)).session_id

        await agent_session.send_user_message("Say 'one'")
        await wait_for_idle(agent_session, timeout=90)
        assert (await agent_session.get_agent_info()).session_id == first_id

        await agent_session.send_user_message("Say 'two'")
        await wait_for_idle(agent_session, timeout=90)
        assert (await agent_session.get_agent_info()).session_id == first_id

    async def test_resume_session(
        self, claude_only, make_session, e2e_project_dir,
    ) -> None:
        """Resuming a session with --resume should work (claude)."""
        session1 = make_session(e2e_project_dir)
        await session1.start(timeout=45)
        # A turn must happen so a resumable transcript is persisted.
        await session1.send_user_message("Remember the word 'banana'.")
        await wait_for_idle(session1, timeout=90)
        original_id = (await session1.get_agent_info()).session_id
        await session1.stop()

        session2 = make_session(e2e_project_dir, resume_session_id=original_id)
        await session2.start(timeout=45)

        try:
            info = await session2.get_agent_info()
            assert info is not None
            assert original_id in str(info.transcript_path)
        finally:
            await session2.stop()

    async def test_worktree_session(
        self, require_capability, make_session, e2e_project_dir,
    ) -> None:
        """Starting with --worktree should write events to the worktree path."""
        require_capability("worktree")
        session = make_session(e2e_project_dir, worktree="test-wt")
        await session.start(timeout=45)

        try:
            info = await session.get_agent_info()
            assert info is not None
            assert info.status == AgentStatus.IDLE

            # Verify the project dir points to the worktree.
            assert "worktrees/test-wt" in str(session.project_dir)

            # Verify tmux session name includes the worktree suffix.
            assert "-worktree-test-wt" in await session.get_tmux_session_name()

            # Send a message and verify it works.
            await session.send_user_message("Say 'worktree test'")
            await wait_for_idle(session, timeout=90)

            # Verify events were written to the worktree events path.
            events = read_events(session)
            event_names = [e["event"] for e in events]
            assert "SessionStart" in event_names
            assert "Stop" in event_names
        finally:
            await session.stop()
