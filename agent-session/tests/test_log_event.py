import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "src" / "agent_session" / "plugin" / "log-event.sh"


def run_hook(home: Path, event: str, payload: dict | str) -> dict:
    env = os.environ.copy()
    # Exercise the $HOME fallback: an ASN_HOME inherited from the environment
    # (an operator's second daemon, or the e2e suite's isolation) would send the
    # events somewhere other than the fake home under test.
    env.pop("ASN_HOME", None)
    env["HOME"] = str(home)
    env["AGENT_SESSION_KEY"] = "test-key"
    subprocess.run(
        ["bash", str(SCRIPT), event],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        text=True,
        env=env,
        check=True,
        stderr=subprocess.DEVNULL,
    )
    events_file = home / ".agent-session" / "sessions" / "test-key" / "events.jsonl"
    lines = events_file.read_text().splitlines()
    return json.loads(lines[-1])


class TestStopBackgrounded:
    def test_stop_with_background_tasks_is_backgrounded(self, tmp_path: Path) -> None:
        entry = run_hook(tmp_path, "Stop", {
            "session_id": "s1",
            "background_tasks": [
                {"type": "shell", "description": "sleep", "command": "sleep 60"},
                {"type": "subagent", "description": "explore"},
            ],
            "session_crons": [{"id": "c1"}],
        })
        assert entry["status"] == "backgrounded"
        assert entry["background_tasks"] == 2
        assert entry["session_crons"] == 1

    def test_stop_failure_with_background_tasks_is_backgrounded(self, tmp_path: Path) -> None:
        entry = run_hook(tmp_path, "StopFailure", {
            "session_id": "s1",
            "background_tasks": [{"type": "workflow", "description": "wf"}],
            "session_crons": [],
        })
        assert entry["status"] == "backgrounded"
        assert entry["background_tasks"] == 1
        assert entry["session_crons"] == 0

    def test_stop_without_fields_is_idle(self, tmp_path: Path) -> None:
        # Older Claude versions / codex do not send the fields at all.
        entry = run_hook(tmp_path, "Stop", {"session_id": "s1"})
        assert entry["status"] == "idle"
        assert entry["background_tasks"] == 0
        assert entry["session_crons"] == 0

    def test_stop_with_empty_arrays_is_idle(self, tmp_path: Path) -> None:
        entry = run_hook(tmp_path, "Stop", {
            "session_id": "s1",
            "background_tasks": [],
            "session_crons": [],
        })
        assert entry["status"] == "idle"
        assert entry["background_tasks"] == 0
        assert entry["session_crons"] == 0

    def test_other_events_unaffected(self, tmp_path: Path) -> None:
        entry = run_hook(tmp_path, "SessionStart", {"session_id": "s1"})
        assert entry["status"] == "idle"
        entry = run_hook(tmp_path, "PreToolUse", {"session_id": "s1"})
        assert entry["status"] == "working"
        entry = run_hook(tmp_path, "SessionEnd", {"session_id": "s1"})
        assert entry["status"] == "ended"

    def test_malformed_payload_still_logs_event(self, tmp_path: Path) -> None:
        # jq fails on non-JSON input; the event must still be logged as idle.
        entry = run_hook(tmp_path, "Stop", "not json {")
        assert entry["status"] == "idle"
        assert entry["background_tasks"] == 0
        assert entry["session_crons"] == 0

    def test_stop_keeps_assistant_message(self, tmp_path: Path) -> None:
        entry = run_hook(tmp_path, "Stop", {
            "session_id": "s1",
            "last_assistant_message": "done",
            "background_tasks": [{"type": "shell"}],
        })
        assert entry["status"] == "backgrounded"
        assert entry["assistant_message"] == "done"
