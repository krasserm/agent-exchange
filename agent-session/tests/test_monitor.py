import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from agent_session._models import AgentStatus
from agent_session._monitor import EventMonitor, watch_events, watch_jsonl
from conftest import make_event_line

# Time to wait after writing events for watchfiles to detect the change.
_DETECT_DELAY = 1.0


def append_event(path: Path, event_name: str, session_id: str = "test-session-id", **extra: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(make_event_line(event_name, session_id=session_id, **extra) + "\n")


class TestWatchEvents:
    async def test_tail_mode_skips_existing(self, events_file: Path) -> None:
        append_event(events_file, "SessionStart", session_id="old")

        collected: list[str] = []

        async def consume() -> None:
            async for event in watch_events(events_file, tail=True):
                collected.append(event.session_id)
                break

        task = asyncio.create_task(consume())
        await asyncio.sleep(_DETECT_DELAY)

        append_event(events_file, "Stop", session_id="new")
        await asyncio.wait_for(task, timeout=10)

        assert collected == ["new"]

    async def test_replay_mode(self, events_file: Path) -> None:
        append_event(events_file, "SessionStart", session_id="s1")
        append_event(events_file, "Stop", session_id="s1")

        collected: list[str] = []

        async def consume() -> None:
            async for event in watch_events(events_file, tail=False):
                collected.append(event.event)
                if len(collected) == 2:
                    break

        await asyncio.wait_for(consume(), timeout=10)
        assert collected == ["SessionStart", "Stop"]

    async def test_file_not_exists_waits(self, events_dir: Path) -> None:
        events_file = events_dir / "events.jsonl"
        assert not events_file.exists()

        collected: list[str] = []

        async def consume() -> None:
            async for event in watch_events(events_file, tail=False):
                collected.append(event.event)
                break

        task = asyncio.create_task(consume())
        await asyncio.sleep(_DETECT_DELAY)

        append_event(events_file, "SessionStart")
        await asyncio.wait_for(task, timeout=10)

        assert collected == ["SessionStart"]

    async def test_malformed_json_skipped(self, events_file: Path) -> None:
        events_file.write_text("")

        collected: list[str] = []

        async def consume() -> None:
            async for event in watch_events(events_file, tail=True):
                collected.append(event.event)
                break

        task = asyncio.create_task(consume())
        await asyncio.sleep(_DETECT_DELAY)

        with events_file.open("a") as f:
            f.write("not-json\n")
            f.write(make_event_line("Stop") + "\n")

        await asyncio.wait_for(task, timeout=10)
        assert collected == ["Stop"]

    async def test_stop_event(self, events_file: Path) -> None:
        events_file.write_text("")
        stop = asyncio.Event()

        collected: list[str] = []

        async def consume() -> None:
            async for event in watch_events(events_file, tail=True, stop_event=stop):
                collected.append(event.event)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.5)
        stop.set()
        await asyncio.wait_for(task, timeout=10)

        assert collected == []


class TestWatchJsonl:
    """The outbox tailer: same offset/inode-safe engine, raw dicts out."""

    async def test_tail_yields_appended_objects(self, events_dir: Path) -> None:
        import json

        outbox = events_dir / "outbox.jsonl"
        outbox.write_text("")

        collected: list[dict] = []

        async def consume() -> None:
            async for obj in watch_jsonl(outbox, tail=True):
                collected.append(obj)
                break

        task = asyncio.create_task(consume())
        await asyncio.sleep(_DETECT_DELAY)

        with outbox.open("a") as f:
            f.write(json.dumps({"to": "peerkey", "message": "hi"}) + "\n")

        await asyncio.wait_for(task, timeout=10)
        assert collected == [{"to": "peerkey", "message": "hi"}]

    async def test_malformed_line_skipped(self, events_dir: Path) -> None:
        import json

        outbox = events_dir / "outbox.jsonl"
        outbox.write_text("")

        collected: list[dict] = []

        async def consume() -> None:
            async for obj in watch_jsonl(outbox, tail=True):
                collected.append(obj)
                break

        task = asyncio.create_task(consume())
        await asyncio.sleep(_DETECT_DELAY)

        with outbox.open("a") as f:
            f.write("not-json\n")
            f.write(json.dumps({"to": "k", "message": "m"}) + "\n")

        await asyncio.wait_for(task, timeout=10)
        assert collected == [{"to": "k", "message": "m"}]


class TestEventMonitor:
    async def test_state_updates(self, events_file: Path) -> None:
        events_file.write_text("")
        monitor = EventMonitor(events_file)
        monitor.start()
        try:
            await asyncio.sleep(0.5)

            append_event(events_file, "SessionStart", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)

            assert monitor.session_id == "s1"
            assert monitor.status == AgentStatus.IDLE
            assert monitor.is_active is True
        finally:
            await monitor.stop()

    async def test_session_end_clears_state(self, events_file: Path) -> None:
        events_file.write_text("")
        monitor = EventMonitor(events_file)
        monitor.start()
        try:
            await asyncio.sleep(0.5)

            append_event(events_file, "SessionStart", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            assert monitor.is_active is True

            append_event(events_file, "SessionEnd", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)

            assert monitor.session_id is None
            assert monitor.status is None
            assert monitor.is_active is False
        finally:
            await monitor.stop()

    async def test_session_id_changes(self, events_file: Path) -> None:
        events_file.write_text("")
        monitor = EventMonitor(events_file)
        monitor.start()
        try:
            await asyncio.sleep(0.5)

            append_event(events_file, "SessionStart", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            assert monitor.session_id == "s1"

            append_event(events_file, "SessionEnd", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            append_event(events_file, "SessionStart", session_id="s2")
            await asyncio.sleep(_DETECT_DELAY)

            assert monitor.session_id == "s2"
            assert monitor.status == AgentStatus.IDLE
        finally:
            await monitor.stop()

    async def test_status_transitions(self, events_file: Path) -> None:
        events_file.write_text("")
        monitor = EventMonitor(events_file)
        monitor.start()
        try:
            await asyncio.sleep(0.5)

            append_event(events_file, "SessionStart", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            assert monitor.status == AgentStatus.IDLE

            append_event(events_file, "UserPromptSubmit", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            assert monitor.status == AgentStatus.WORKING

            append_event(events_file, "Elicitation", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            assert monitor.status == AgentStatus.WAITING

            append_event(events_file, "Stop", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            assert monitor.status == AgentStatus.IDLE
        finally:
            await monitor.stop()

    async def test_subagent_stop_does_not_overwrite_idle(self, events_file: Path) -> None:
        events_file.write_text("")
        monitor = EventMonitor(events_file)
        monitor.start()
        try:
            await asyncio.sleep(0.5)

            append_event(events_file, "SessionStart", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)

            append_event(events_file, "UserPromptSubmit", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            assert monitor.status == AgentStatus.WORKING

            append_event(events_file, "Stop", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            assert monitor.status == AgentStatus.IDLE

            append_event(events_file, "SubagentStop", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            assert monitor.status == AgentStatus.IDLE
        finally:
            await monitor.stop()

    async def test_subagent_events_do_not_disturb_in_turn_status(self, events_file: Path) -> None:
        events_file.write_text("")
        monitor = EventMonitor(events_file)
        monitor.start()
        try:
            await asyncio.sleep(0.5)
            append_event(events_file, "SessionStart", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)

            append_event(events_file, "UserPromptSubmit", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            assert monitor.status == AgentStatus.WORKING

            append_event(events_file, "SubagentStart", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            assert monitor.status == AgentStatus.WORKING

            append_event(events_file, "SubagentStop", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            assert monitor.status == AgentStatus.WORKING
        finally:
            await monitor.stop()

    async def test_backgrounded_stop_updates_status_and_counts(self, events_file: Path) -> None:
        events_file.write_text("")
        monitor = EventMonitor(events_file)
        monitor.start()
        try:
            await asyncio.sleep(0.5)

            append_event(events_file, "SessionStart", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            assert monitor.background_tasks == 0
            assert monitor.session_crons == 0

            append_event(
                events_file, "Stop", session_id="s1",
                status="backgrounded", background_tasks=2, session_crons=1,
            )
            await asyncio.sleep(_DETECT_DELAY)
            assert monitor.status == AgentStatus.BACKGROUNDED
            assert monitor.background_tasks == 2
            assert monitor.session_crons == 1

            # A plain Stop (background work finished) folds back to idle.
            append_event(events_file, "Stop", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            assert monitor.status == AgentStatus.IDLE
            assert monitor.background_tasks == 0
            assert monitor.session_crons == 0
        finally:
            await monitor.stop()

    async def test_subagent_events_do_not_disturb_backgrounded(self, events_file: Path) -> None:
        events_file.write_text("")
        monitor = EventMonitor(events_file)
        monitor.start()
        try:
            await asyncio.sleep(0.5)

            append_event(
                events_file, "Stop", session_id="s1",
                status="backgrounded", background_tasks=1, session_crons=0,
            )
            await asyncio.sleep(_DETECT_DELAY)
            assert monitor.status == AgentStatus.BACKGROUNDED

            append_event(events_file, "SubagentStop", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            assert monitor.status == AgentStatus.BACKGROUNDED
            assert monitor.background_tasks == 1
        finally:
            await monitor.stop()

    async def test_wait_for_event(self, events_file: Path) -> None:
        events_file.write_text("")
        monitor = EventMonitor(events_file)
        monitor.start()
        try:
            async def write_later() -> None:
                await asyncio.sleep(_DETECT_DELAY)
                append_event(events_file, "SessionStart", session_id="s1")

            asyncio.create_task(write_later())
            event = await monitor.wait_for_event("SessionStart", timeout=10)

            assert event.event == "SessionStart"
            assert event.session_id == "s1"
        finally:
            await monitor.stop()

    async def test_wait_for_event_timeout(self, events_file: Path) -> None:
        events_file.write_text("")
        monitor = EventMonitor(events_file)
        monitor.start()
        try:
            with pytest.raises(TimeoutError):
                await monitor.wait_for_event("SessionStart", timeout=1.0)
        finally:
            await monitor.stop()


class TestAssistantMessageBuffer:
    async def test_stop_event_captures_assistant_message(self, events_file: Path) -> None:
        events_file.write_text("")
        monitor = EventMonitor(events_file)
        monitor.start()
        try:
            await asyncio.sleep(0.5)
            append_event(events_file, "SessionStart", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            append_event(
                events_file, "Stop", session_id="s1",
                assistant_message="Hello there!",
            )
            await asyncio.sleep(_DETECT_DELAY)

            msgs = monitor.get_assistant_messages(last=1)
            assert len(msgs) == 1
            assert msgs[0].message == "Hello there!"
            assert isinstance(msgs[0].timestamp, datetime)
        finally:
            await monitor.stop()

    async def test_multiple_messages(self, events_file: Path) -> None:
        events_file.write_text("")
        monitor = EventMonitor(events_file)
        monitor.start()
        try:
            await asyncio.sleep(0.5)
            append_event(events_file, "SessionStart", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)

            for i in range(3):
                append_event(events_file, "UserPromptSubmit", session_id="s1")
                await asyncio.sleep(0.3)
                append_event(
                    events_file, "Stop", session_id="s1",
                    assistant_message=f"msg-{i}",
                )
                await asyncio.sleep(_DETECT_DELAY)

            msgs = monitor.get_assistant_messages(last=2)
            assert len(msgs) == 2
            assert msgs[0].message == "msg-1"
            assert msgs[1].message == "msg-2"
        finally:
            await monitor.stop()

    async def test_events_without_assistant_message_ignored(self, events_file: Path) -> None:
        events_file.write_text("")
        monitor = EventMonitor(events_file)
        monitor.start()
        try:
            await asyncio.sleep(0.5)
            append_event(events_file, "SessionStart", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            append_event(events_file, "UserPromptSubmit", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)

            msgs = monitor.get_assistant_messages(last=10)
            assert len(msgs) == 0
        finally:
            await monitor.stop()

    async def test_last_exceeds_buffer(self, events_file: Path) -> None:
        events_file.write_text("")
        monitor = EventMonitor(events_file)
        monitor.start()
        try:
            await asyncio.sleep(0.5)
            append_event(events_file, "SessionStart", session_id="s1")
            await asyncio.sleep(_DETECT_DELAY)
            append_event(
                events_file, "Stop", session_id="s1",
                assistant_message="only one",
            )
            await asyncio.sleep(_DETECT_DELAY)

            msgs = monitor.get_assistant_messages(last=100)
            assert len(msgs) == 1
        finally:
            await monitor.stop()
