"""E2E tests: send_user_message, get_assistant_messages with real Claude Code."""

import asyncio

import pytest

from agent_session import AgentSession

from .helpers import read_events, wait_for_idle

pytestmark = pytest.mark.e2e


class TestCapture:
    async def test_returns_terminal_output(self, agent_session: AgentSession) -> None:
        """After start(), capture() should return non-empty terminal content."""
        output = await agent_session.capture()
        assert isinstance(output, str)
        assert len(output.strip()) > 0


class TestSendAndGetMessages:
    async def test_send_and_retrieve_assistant_message(
        self, agent_session: AgentSession,
    ) -> None:
        """Send a message, wait for idle, then get_assistant_messages should return it."""
        await agent_session.send_user_message(
            "Reply with exactly: hello world"
        )
        await wait_for_idle(agent_session, timeout=120)

        messages = await agent_session.get_assistant_messages(last=1)
        assert len(messages) >= 1
        assert "hello world" in messages[-1].message.lower()

    async def test_sequential_messages(
        self, agent_session: AgentSession,
    ) -> None:
        """Two sequential messages should produce two assistant messages."""
        await agent_session.send_user_message("Reply with exactly: alpha")
        await wait_for_idle(agent_session, timeout=120)

        await agent_session.send_user_message("Reply with exactly: beta")
        await wait_for_idle(agent_session, timeout=120)

        messages = await agent_session.get_assistant_messages(last=2)
        assert len(messages) >= 2
        texts = [m.message.lower() for m in messages[-2:]]
        assert any("alpha" in t for t in texts)
        assert any("beta" in t for t in texts)


class TestMultilineMessage:
    """A multi-line message must reach the agent as ONE submission.

    Typed as keystrokes, the bare LF between lines is read by the composer as
    "submit": the first line leaves as its own turn and the remainder lands in a
    fresh composer, so the agent sees half a question. ``send_text`` pastes
    through a tmux buffer to avoid exactly that; nothing exercised it end to end
    until here.

    The assertion reads the ``UserPromptSubmit`` event, which carries the text
    that was actually submitted. That is the whole question -- and it avoids
    waiting on the model to reply, which made an earlier version of this test
    fail on codex for taking longer than the timeout rather than for splitting
    anything.
    """

    async def test_arrives_as_a_single_turn(self, agent_session: AgentSession) -> None:
        def prompts() -> list[dict]:
            return [
                e for e in read_events(agent_session)
                if e["event"] == "UserPromptSubmit"
            ]

        before = len(prompts())
        # The distinguishing token is on the LAST line: if the message were split
        # at a newline, the first submission would carry only the first line.
        await agent_session.send_user_message(
            "Reply with exactly the word on the last line of this message.\n"
            "not-this-one\n"
            "zebra-42"
        )

        deadline = asyncio.get_event_loop().time() + 60
        while asyncio.get_event_loop().time() < deadline:
            if len(prompts()) > before:
                break
            await asyncio.sleep(0.5)

        new = prompts()[before:]
        assert new, "no UserPromptSubmit event: nothing was submitted at all"
        submitted = new[0].get("user_message", "")
        assert "not-this-one" in submitted and "zebra-42" in submitted, (
            f"the submission was truncated at a newline: {submitted!r}"
        )
        # Then settle, and confirm the tail did not arrive as further turns.
        await asyncio.sleep(3)
        after = prompts()[before:]
        assert len(after) == 1, (
            "the message was split across submissions: "
            f"{[p.get('user_message') for p in after]}"
        )
