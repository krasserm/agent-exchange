"""E2E tests: cron trigger creation."""

import pytest

from agent_session import AgentSession

from .helpers import read_events, wait_for_idle

pytestmark = pytest.mark.e2e


class TestCron:
    async def test_cron_create_events(
        self, claude_only, agent_session: AgentSession,
    ) -> None:
        """Creating and deleting a cron trigger should produce tool events."""
        await agent_session.send_user_message(
            "Create a cron trigger with a 1-minute interval. "
            "The trigger prompt should be: 'Say hello'. "
            "Then immediately list all cron triggers. "
            "Then delete the cron trigger you just created."
        )
        await wait_for_idle(agent_session, timeout=120)

        events = read_events(agent_session)
        event_names = [e["event"] for e in events]

        # Should have tool use events from CronCreate, CronList, CronDelete.
        assert "PreToolUse" in event_names, f"No PreToolUse event. Events: {event_names}"
        assert "PostToolUse" in event_names, f"No PostToolUse event. Events: {event_names}"
