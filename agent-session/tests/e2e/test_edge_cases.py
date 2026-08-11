"""E2E tests: edge cases, external kills, error scenarios."""

import asyncio
import shutil

import libtmux
import pytest

from agent_session import AgentSession, AgentStatus

from .helpers import (
    send_via_tmux,
    wait_for_idle,
    wait_for_info_none,
    wait_for_status,
)

pytestmark = pytest.mark.e2e


class TestEdgeCases:
    async def test_external_tmux_kill(self, make_session, e2e_project_dir) -> None:
        """Killing tmux externally should be detected."""
        session = make_session(e2e_project_dir)
        await session.start(timeout=45)

        tmux_name = await session.get_tmux_session_name()
        assert tmux_name is not None

        server = libtmux.Server()
        tmux_session = server.sessions.get(session_name=tmux_name, default=None)
        assert tmux_session is not None
        tmux_session.kill()

        assert await session.get_tmux_session_name() is None
        await session.stop()

    async def test_stop_during_working(self, make_session, e2e_project_dir) -> None:
        """Stopping while the agent is working should not raise."""
        session = make_session(e2e_project_dir)
        await session.start(timeout=45)

        await session.send_user_message(
            "Write a detailed analysis of the Python standard library asyncio "
            "module, covering at least 10 different functions. Be very thorough."
        )

        try:
            await wait_for_status(session, AgentStatus.WORKING, timeout=15)
        except TimeoutError:
            pass

        await session.stop()

    async def test_send_after_exit(
        self, claude_only, make_session, e2e_project_dir,
    ) -> None:
        """Sending a message after /exit should raise (claude)."""
        session = make_session(e2e_project_dir)
        await session.start(timeout=45)

        tmux_name = await session.get_tmux_session_name()
        send_via_tmux(tmux_name, "/exit")
        await wait_for_info_none(session, timeout=30)

        with pytest.raises(RuntimeError):
            await session.send_user_message("hello")

        await session.stop()

    async def test_rapid_prompts(self, agent_session: AgentSession) -> None:
        """Sending prompts in quick succession should work."""
        await agent_session.send_user_message("Say 'first'")
        await wait_for_idle(agent_session, timeout=90)

        await agent_session.send_user_message("Say 'second'")
        await wait_for_idle(agent_session, timeout=90)

        info = await agent_session.get_agent_info()
        assert info is not None
        assert info.status == AgentStatus.IDLE

    async def test_double_stop(self, make_session, e2e_project_dir) -> None:
        """Calling stop() twice should not raise."""
        session = make_session(e2e_project_dir)
        await session.start(timeout=45)

        await session.stop()
        await session.stop()

    async def test_start_two_sessions_same_dir(
        self, make_session, e2e_project_dir,
    ) -> None:
        """Starting two sessions in the same dir should work (session isolation)."""
        session1 = make_session(e2e_project_dir)
        await session1.start(timeout=45)

        try:
            session2 = make_session(e2e_project_dir)
            await session2.start(timeout=45)

            try:
                assert await session1.get_tmux_session_name() != await session2.get_tmux_session_name()
                assert await session1.get_agent_info() is not None
                assert await session2.get_agent_info() is not None
            finally:
                await session2.stop()
        finally:
            await session1.stop()

    async def test_project_dir_deleted(self, make_session, e2e_project_dir) -> None:
        """get_agent_info() should return None when the project dir is deleted."""
        session = make_session(e2e_project_dir)
        await session.start(timeout=45)

        try:
            info = await session.get_agent_info()
            assert info is not None

            # Delete the project dir (simulates worktree removal).
            shutil.rmtree(e2e_project_dir)

            assert await session.get_agent_info() is None
        finally:
            await session.stop()

    async def test_worktree_exit_with_removal(
        self, require_capability, make_session, e2e_project_dir,
    ) -> None:
        """After /exit with worktree removal, get_agent_info() should return None."""
        require_capability("worktree")
        session = make_session(e2e_project_dir, worktree="removal-test")
        await session.start(timeout=45)

        info = await session.get_agent_info()
        assert info is not None
        worktree_dir = session.project_dir
        assert worktree_dir.exists()

        # /exit via native UI. Claude Code may prompt to remove the worktree;
        # with --dangerously-skip-permissions it auto-confirms removal.
        tmux_name = await session.get_tmux_session_name()
        send_via_tmux(tmux_name, "/exit")
        await wait_for_info_none(session, timeout=30)

        # Whether or not the worktree was actually removed, get_agent_info()
        # must return None after session end.
        assert await session.get_agent_info() is None

        await session.stop()

    async def test_no_ghost_dirs_after_session_end(
        self, make_session, e2e_project_dir,
    ) -> None:
        """A session-end hook should not recreate a deleted project dir."""
        session = make_session(e2e_project_dir)
        await session.start(timeout=45)

        # Delete the project dir BEFORE stopping (simulates worktree removal
        # during /exit). Any session-end hook that fires when stop() kills the
        # tmux session should not recreate it.
        shutil.rmtree(e2e_project_dir)

        await session.stop()
        await asyncio.sleep(2)

        assert not e2e_project_dir.exists(), (
            f"{e2e_project_dir} was recreated after deletion (ghost dir from hook)"
        )
