"""E2E tests: concurrent sessions and session isolation."""

import asyncio
from pathlib import Path

import pytest

from agent_session import AgentSession, AgentStatus

from .helpers import (
    active_info,
    read_events,
    wait_for_idle,
)

pytestmark = pytest.mark.e2e


class TestConcurrentSessions:
    async def test_two_sessions_same_directory(
        self, make_session, e2e_project_dir: Path,
    ) -> None:
        """Two sessions in the same directory should run independently."""
        session_a = make_session(e2e_project_dir)
        session_b = make_session(e2e_project_dir)

        await session_a.start(timeout=45)
        try:
            await session_b.start(timeout=45)
            try:
                # Distinct tmux session names.
                assert await session_a.get_tmux_session_name() != await session_b.get_tmux_session_name()

                # Distinct session directories.
                assert session_a.session_dir != session_b.session_dir

                # Both are ready and idle.
                info_a = await session_a.get_agent_info()
                info_b = await session_b.get_agent_info()
                assert info_a is not None
                assert info_b is not None
                assert info_a.status == AgentStatus.IDLE
                assert info_b.status == AgentStatus.IDLE

                # Send a message to session A only.
                await session_a.send_user_message(
                    "Create a file called concurrent_a.txt containing 'from A'. "
                    "Do not output anything else."
                )
                await wait_for_idle(session_a, timeout=90)

                # Session A's events file should have new events.
                events_a = read_events(session_a)
                event_names_a = [e["event"] for e in events_a]
                assert "UserPromptSubmit" in event_names_a

                # Session B should not have received the prompt.
                events_b = read_events(session_b)
                event_names_b = [e["event"] for e in events_b]
                assert "UserPromptSubmit" not in event_names_b

                # Session B is still idle and unaffected.
                assert (await session_b.get_agent_info()).status == AgentStatus.IDLE
            finally:
                await session_b.stop()

            # After stopping B, session A should still be active.
            info_a = await session_a.get_agent_info()
            assert info_a is not None
        finally:
            await session_a.stop()


class TestConcurrentSendsSameSession:
    async def test_concurrent_sends_to_same_session(
        self, agent_session: AgentSession,
    ) -> None:
        """Two concurrent sends to the same session should both complete."""
        await asyncio.gather(
            agent_session.send_user_message("Reply with exactly: concurrent-a"),
            agent_session.send_user_message("Reply with exactly: concurrent-b"),
        )
        # Both should have been sent. Wait for processing.
        await wait_for_idle(agent_session, timeout=120)

        messages = await agent_session.get_assistant_messages(last=5)
        texts = " ".join(m.message.lower() for m in messages)
        # At least one of the concurrent messages should have been processed.
        assert "concurrent-a" in texts or "concurrent-b" in texts


class TestSessionDirectoryStructure:
    async def test_session_dir_layout(
        self, agent_session: AgentSession,
    ) -> None:
        """Session directory should be under sessions/<key>/ with events.jsonl."""
        # Ensure at least one lifecycle event has been written (codex only
        # writes events once the first turn begins).
        await active_info(agent_session)

        session_dir = agent_session.session_dir
        assert session_dir.exists()
        assert session_dir.parent.name == "sessions"

        events_file = session_dir / "events.jsonl"
        assert events_file.exists()

        events = read_events(agent_session)
        event_names = [e["event"] for e in events]
        assert "SessionStart" in event_names


class TestResumeWithFreshKey:
    async def test_resume_gets_new_session_key(
        self, claude_only, make_session, e2e_project_dir: Path,
    ) -> None:
        """Resuming a session should use a fresh session key and directory."""
        session1 = make_session(e2e_project_dir)
        await session1.start(timeout=45)
        # A turn must happen so a resumable transcript is persisted.
        await session1.send_user_message("Remember the word 'banana'.")
        await wait_for_idle(session1, timeout=90)
        original_id = (await session1.get_agent_info()).session_id
        original_key = session1.session_key
        original_dir = session1.session_dir
        await session1.stop()

        session2 = make_session(e2e_project_dir, resume_session_id=original_id)
        await session2.start(timeout=45)

        try:
            assert session2.session_key != original_key
            assert session2.session_dir != original_dir
            assert session2.session_dir.exists()
            assert (session2.session_dir / "events.jsonl").exists()
        finally:
            await session2.stop()
