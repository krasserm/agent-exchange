"""Unit tests for the daemon client's auto-start policy.

Read-only/maintenance calls must never spawn a daemon: a poll (e.g. from the
web UI or `asn-top`) against a dead daemon should report "nothing running",
not resurrect one. Only explicit mutating calls (`start`) auto-start.
"""

from unittest.mock import AsyncMock

import pytest

from agent_session._client import DaemonClient, DaemonNotRunningError


class TestAutoStartPolicy:
    async def test_read_only_does_not_autostart(self, monkeypatch) -> None:
        client = DaemonClient()

        async def fail(*_args, **_kwargs):
            raise ConnectionRefusedError()

        ensure = AsyncMock()
        monkeypatch.setattr(client, "_send", fail)
        monkeypatch.setattr(client, "_ensure_daemon", ensure)

        with pytest.raises(DaemonNotRunningError):
            await client.send("list")

        ensure.assert_not_called()

    async def test_start_autostarts_then_retries(self, monkeypatch) -> None:
        client = DaemonClient()
        calls = {"n": 0}

        async def first_fails(_method, _params, _timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionRefusedError()
            return {"ok": True}

        ensure = AsyncMock()
        monkeypatch.setattr(client, "_send", first_fails)
        monkeypatch.setattr(client, "_ensure_daemon", ensure)

        result = await client.send("start", {}, auto_start=True)

        ensure.assert_called_once()
        assert result == {"ok": True}

    async def test_not_running_error_is_daemon_error(self) -> None:
        from agent_session._client import DaemonError

        assert issubclass(DaemonNotRunningError, DaemonError)
