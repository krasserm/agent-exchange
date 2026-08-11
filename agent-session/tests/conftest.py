"""Shared fixtures, plus the state-root isolation the whole suite depends on."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Point the daemon's state root, socket and read endpoint away from the user's
# live tree -- for EVERY run. This is the topmost conftest, so pytest imports it
# before collecting any test module, and therefore before anything imports
# ``agent_session``, which resolves all three once at import time.
#
# This lived in tests/e2e/__init__.py, where it only ran when pytest collected
# tests/e2e: `pytest --ignore=tests/e2e` and single-file runs wrote session dirs
# into the user's real ~/.agent-session instead. The integration tests under
# tests/ create session dirs too, and they have no business doing that in the
# live tree either.
#
# Without the override the e2e suite drives the live daemon outright: `asn daemon
# stop` and `asn stop --all` (cleanup_daemon runs the former after every test)
# stop every session in that daemon's registry, not just the ones a test
# started. tests/e2e/conftest.py re-checks in pytest_configure that the
# overrides actually took effect before any test runs.
E2E_HOME = Path("~/.agent-session-e2e").expanduser()
E2E_SOCKET = Path(f"/tmp/agent-session-e2e-{os.getuid()}.sock")
E2E_READ_PORT = "47601"

os.environ["ASN_HOME"] = str(E2E_HOME)
os.environ["ASN_SOCKET_PATH"] = str(E2E_SOCKET)
os.environ.setdefault("ASN_READ_PORT", E2E_READ_PORT)


EVENT_STATUS_MAP = {
    "SessionEnd": "ended",
    "Stop": "idle",
    "StopFailure": "idle",
    "TeammateIdle": "idle",
    "TaskCompleted": "idle",
    "SessionStart": "idle",
    "Elicitation": "waiting_for_input",
    "PermissionRequest": "waiting_for_input",
}


def make_event_line(
    event_name: str,
    session_id: str = "test-session-id",
    agent_id: str = "n/a",
    agent_type: str = "n/a",
    **extra: str,
) -> str:
    status = EVENT_STATUS_MAP.get(event_name, "working")
    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event_name,
        "status": status,
        "session_id": session_id,
        "agent_id": agent_id,
        "agent_type": agent_type,
        **extra,
    }
    return json.dumps(data)


@pytest.fixture
def events_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".claude"
    d.mkdir()
    return d


@pytest.fixture
def events_file(events_dir: Path) -> Path:
    return events_dir / "events.jsonl"
