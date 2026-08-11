"""E2E tests: interactions via native terminal UI (tmux send-keys)."""

import asyncio

import pytest

from agent_session import AgentSession, AgentStatus

from .helpers import (
    capture_pane,
    send_via_tmux,
    wait_for_idle,
    wait_for_info_none,
    wait_for_status,
)

pytestmark = pytest.mark.e2e


class TestNativeUI:
    async def test_exit_via_native_ui(
        self, claude_only, agent_session: AgentSession,
    ) -> None:
        """Sending /exit via tmux should end the Claude session."""
        tmux_name = await agent_session.get_tmux_session_name()
        assert tmux_name is not None

        send_via_tmux(tmux_name, "/exit")
        await wait_for_info_none(agent_session, timeout=30)

        # The tmux session should still exist (Claude exited, shell remains).
        assert await agent_session.get_tmux_session_name() is not None

    async def test_prompt_via_native_ui(
        self, agent_session: AgentSession,
    ) -> None:
        """Sending a prompt directly via tmux should be tracked."""
        tmux_name = await agent_session.get_tmux_session_name()
        assert tmux_name is not None

        send_via_tmux(tmux_name, "What is 2+2? Answer with just the number.")

        await wait_for_status(agent_session, AgentStatus.WORKING, timeout=10)
        await wait_for_idle(agent_session, timeout=60)

        output = capture_pane(tmux_name)
        assert "4" in output

    async def test_clear_via_native_ui(
        self, claude_only, agent_session: AgentSession,
    ) -> None:
        """Sending /clear via tmux should start a new session with a new ID."""
        original_id = (await agent_session.get_agent_info()).session_id
        tmux_name = await agent_session.get_tmux_session_name()

        send_via_tmux(tmux_name, "/clear")

        # Wait for the session to cycle: end -> new start -> idle.
        await asyncio.sleep(5)
        deadline = asyncio.get_event_loop().time() + 30
        new_id = None
        while asyncio.get_event_loop().time() < deadline:
            info = await agent_session.get_agent_info()
            if info is not None and info.session_id != original_id:
                new_id = info.session_id
                break
            await asyncio.sleep(0.5)

        assert new_id is not None, "Session ID did not change after /clear"
        assert new_id != original_id

    async def test_mixed_interfaces(
        self, agent_session: AgentSession,
    ) -> None:
        """Mixing AgentSession.send_user_message and native tmux input should work."""
        await agent_session.send_user_message("Say 'alpha'")
        await wait_for_idle(agent_session, timeout=60)

        tmux_name = await agent_session.get_tmux_session_name()
        send_via_tmux(tmux_name, "Say 'beta'")
        await wait_for_idle(agent_session, timeout=60)

        output = capture_pane(tmux_name)
        assert "alpha" in output.lower() or "beta" in output.lower()
