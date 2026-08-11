"""Integration tests for provisioning filesystem I/O (local transport, no net)."""

from pathlib import Path

import pytest

from agent_session import _naming, _provision
from agent_session.agents import Transport

LOCAL_NATIVE = Transport()


class TestWriteAgentHostFile:
    def test_writes_file_and_creates_parents(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "a" / "b" / "launch.sh")
        _provision.write_agent_host_file(LOCAL_NATIVE, dest, "echo hi\n", executable=True)
        p = Path(dest)
        assert p.read_text() == "echo hi\n"
        assert p.stat().st_mode & 0o111  # executable bit set

    def test_non_executable_by_default(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "f.txt")
        _provision.write_agent_host_file(LOCAL_NATIVE, dest, "data")
        assert not (Path(dest).stat().st_mode & 0o111)


class TestEnsurePluginLocal:
    @pytest.fixture
    def state_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        # A local agent host deploys into the control host's state root, so the
        # override that isolates a second daemon isolates its plugin too.
        root = tmp_path / "state"
        root.mkdir()
        monkeypatch.setattr(_naming, "DAEMON_DIR", root)
        return root

    def _real_plugin(self) -> Path:
        return Path(__import__("agent_session").__file__).parent / "plugin"

    def test_deploys_plugin_files_and_marker(self, state_root: Path) -> None:
        _provision.ensure_plugin(LOCAL_NATIVE, self._real_plugin(), remote_home=None)
        root = state_root / "plugin"
        for rel in _provision.PLUGIN_FILES:
            assert (root / rel).exists(), rel
        assert (root / ".checksum").exists()
        # the deployed hook + the asn shim are executable
        assert (root / "log-event.sh").stat().st_mode & 0o111
        assert (root / "asn").stat().st_mode & 0o111

    def test_idempotent_skip_when_checksum_matches(
        self, state_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = self._real_plugin()
        _provision.ensure_plugin(LOCAL_NATIVE, src, remote_home=None)

        # Second call must not rewrite (track write calls).
        calls = []
        orig = _provision.write_agent_host_file
        monkeypatch.setattr(
            _provision, "write_agent_host_file",
            lambda *a, **k: calls.append(a) or orig(*a, **k),
        )
        _provision.ensure_plugin(LOCAL_NATIVE, src, remote_home=None)
        assert calls == []  # nothing rewritten

    def test_redeploys_when_marker_stale(self, state_root: Path) -> None:
        src = self._real_plugin()
        _provision.ensure_plugin(LOCAL_NATIVE, src, remote_home=None)
        marker = state_root / "plugin" / ".checksum"
        marker.write_text("stale")
        _provision.ensure_plugin(LOCAL_NATIVE, src, remote_home=None)
        assert marker.read_text() == _provision.plugin_checksum(src)
