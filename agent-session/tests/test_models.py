from datetime import datetime

from agent_session._models import (
    AgentInfo,
    AgentStatus,
    Event,
    EventStatus,
    event_status_to_agent_status,
)


class TestBackgroundedStatus:
    def test_event_status_has_backgrounded(self) -> None:
        assert EventStatus.BACKGROUNDED == "backgrounded"

    def test_agent_status_has_backgrounded(self) -> None:
        assert AgentStatus.BACKGROUNDED == "backgrounded"

    def test_mapping(self) -> None:
        assert event_status_to_agent_status(EventStatus.BACKGROUNDED) is AgentStatus.BACKGROUNDED

    def test_existing_mappings_unchanged(self) -> None:
        assert event_status_to_agent_status(EventStatus.IDLE) is AgentStatus.IDLE
        assert event_status_to_agent_status(EventStatus.WORKING) is AgentStatus.WORKING
        assert event_status_to_agent_status(EventStatus.WAITING_FOR_INPUT) is AgentStatus.WAITING
        assert event_status_to_agent_status(EventStatus.ENDED) is None


class TestCountFields:
    def test_event_counts_default_to_zero(self) -> None:
        event = Event(
            timestamp=datetime.now(),
            event="Stop",
            status=EventStatus.IDLE,
            session_id="s1",
        )
        assert event.background_tasks == 0
        assert event.session_crons == 0

    def test_event_counts_parsed(self) -> None:
        event = Event.model_validate({
            "timestamp": datetime.now().isoformat(),
            "event": "Stop",
            "status": "backgrounded",
            "session_id": "s1",
            "background_tasks": 2,
            "session_crons": 1,
        })
        assert event.status is EventStatus.BACKGROUNDED
        assert event.background_tasks == 2
        assert event.session_crons == 1

    def test_agent_info_counts_default_to_zero(self) -> None:
        info = AgentInfo(session_id="s1", status=AgentStatus.IDLE)
        assert info.background_tasks == 0
        assert info.session_crons == 0
