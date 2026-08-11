"""E2E tests: subagent and multi-tool event tracking."""

import pytest

from agent_session import AgentSession

from .helpers import read_events, wait_for_event_logged, wait_for_idle

pytestmark = pytest.mark.e2e


class TestSubagents:
    async def test_subagent_events(
        self, claude_only, agent_session: AgentSession,
    ) -> None:
        """A prompt triggering a subagent should produce SubagentStart/Stop events."""
        await agent_session.send_user_message(
            "Use an Agent subagent to list all files in the current directory. "
            "Keep it brief."
        )
        await wait_for_idle(agent_session, timeout=120)

        # SubagentStop is emitted ~2s after the turn's Stop, so poll for both
        # rather than reading once right after idle (which would race the write).
        assert await wait_for_event_logged(agent_session, "SubagentStart"), (
            f"No SubagentStart event. Events: "
            f"{[e['event'] for e in read_events(agent_session)]}"
        )
        assert await wait_for_event_logged(agent_session, "SubagentStop"), (
            f"No SubagentStop event. Events: "
            f"{[e['event'] for e in read_events(agent_session)]}"
        )

    async def test_multi_tool_prompt(
        self, agent_session: AgentSession,
    ) -> None:
        """A prompt triggering multiple tool uses should produce multiple tool events."""
        await agent_session.send_user_message(
            "Create three files: a.txt with 'aaa', b.txt with 'bbb', c.txt with 'ccc'. "
            "Create them one at a time."
        )
        await wait_for_idle(agent_session, timeout=90)

        events = read_events(agent_session)
        event_names = [e["event"] for e in events]

        pre_count = event_names.count("PreToolUse")
        post_count = event_names.count("PostToolUse")
        assert pre_count >= 3, f"Expected >=3 PreToolUse, got {pre_count}. Events: {event_names}"
        assert post_count >= 3, f"Expected >=3 PostToolUse, got {post_count}. Events: {event_names}"

        project_dir = agent_session.project_dir
        assert (project_dir / "a.txt").exists()
        assert (project_dir / "b.txt").exists()
        assert (project_dir / "c.txt").exists()
