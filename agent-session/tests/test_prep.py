"""Unit tests for the prep engine's pure logic (no ssh/docker I/O)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_session import _prep
from agent_session._prep import BuildFacts, PrepReport, Status
from agent_session.agents import Transport

MAC = BuildFacts(uname="Darwin", uid=1000, gid=1000)
LINUX = BuildFacts(uname="Linux", uid=1001, gid=1002)

LOCAL_NATIVE = Transport()
LOCAL_CONTAINER = Transport(runtime="container")
REMOTE_NATIVE = Transport(location="remote", host="h")
REMOTE_CONTAINER = Transport(location="remote", host="h", runtime="container")


class TestContextHash:
    def test_stable(self) -> None:
        a = _prep.context_hash(b"FROM node\n", MAC)
        b = _prep.context_hash(b"FROM node\n", MAC)
        assert a == b
        assert len(a) == 64  # sha256 hex

    def test_sensitive_to_dockerfile(self) -> None:
        a = _prep.context_hash(b"FROM node\n", MAC)
        b = _prep.context_hash(b"FROM node:22\n", MAC)
        assert a != b

    def test_sensitive_to_build_facts(self) -> None:
        # A different uid (Linux host user) must change the hash so a uid change
        # triggers a rebuild.
        a = _prep.context_hash(b"FROM node\n", LINUX)
        b = _prep.context_hash(b"FROM node\n", BuildFacts("Linux", 2000, 2000))
        assert a != b

    def test_cache_bust_not_in_hash(self) -> None:
        # context_hash takes no cache-bust input at all -- rebuilds must not read
        # as stale afterwards. (Guards the exclusion by construction.)
        assert _prep.context_hash(b"x", MAC) == _prep.context_hash(b"x", MAC)


class TestBuildImageArgv:
    def test_macos_omits_uid_gid_and_streams_stdin(self) -> None:
        argv = _prep.build_image_argv("agent-docker", facts=MAC, context_hash="abc")
        assert argv[0:4] == ["docker", "build", "-t", "agent-docker"]
        assert "--label" in argv
        assert f"{_prep.IMAGE_HASH_LABEL}=abc" in argv
        assert "--build-arg" not in argv  # no UID/GID on macOS
        assert argv[-1] == "-"  # Dockerfile from stdin, empty context

    def test_linux_includes_uid_gid(self) -> None:
        argv = _prep.build_image_argv("agent-docker", facts=LINUX, context_hash="abc")
        assert "UID=1001" in argv
        assert "GID=1002" in argv
        assert argv[-1] == "-"

    def test_rebuild_adds_cache_bust(self) -> None:
        argv = _prep.build_image_argv(
            "agent-docker", facts=MAC, context_hash="abc", cache_bust=12345
        )
        assert "CLAUDE_CACHE_BUST=12345" in argv
        assert "CODEX_CACHE_BUST=12345" in argv

    def test_no_cache_bust_by_default(self) -> None:
        argv = _prep.build_image_argv("agent-docker", facts=MAC, context_hash="abc")
        assert not any("CACHE_BUST" in a for a in argv)


class TestImageState:
    def test_missing_when_no_label(self) -> None:
        assert _prep.image_state(None, "want") == "missing"

    def test_stale_on_mismatch(self) -> None:
        assert _prep.image_state("have", "want") == "stale"

    def test_up_to_date_on_match(self) -> None:
        assert _prep.image_state("same", "same") == "up-to-date"


class TestDockerfilePath:
    def test_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_DOCKER_DIR", str(tmp_path))
        assert _prep.dockerfile_path() == tmp_path / "Dockerfile"

    def test_default_resolves_monorepo_sibling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AGENT_DOCKER_DIR", raising=False)
        p = _prep.dockerfile_path()
        assert p.name == "Dockerfile"
        assert p.parent.name == "agent-docker"
        # The real monorepo Dockerfile must be locatable for the build to work.
        assert p.is_file()

    def test_bytes_error_when_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENT_DOCKER_DIR", str(tmp_path))
        with pytest.raises(_prep.ProvisionError, match="Dockerfile not found"):
            _prep.dockerfile_bytes()


class TestReport:
    def test_ready_ignores_warnings(self) -> None:
        r = PrepReport("local-native")
        r.add("a", Status.OK)
        r.add("b", Status.WARN, "degraded")
        r.add("c", Status.FIXED)
        assert r.ready is True

    def test_not_ready_on_fail(self) -> None:
        r = PrepReport("remote-container @ h")
        r.add("docker", Status.FAIL, "down", remediation="start docker")
        assert r.ready is False

    def test_to_dict_shape(self) -> None:
        r = PrepReport("local-native")
        r.add("tmux", Status.OK, "on PATH")
        d = r.to_dict()
        assert d["ready"] is True
        assert d["checks"][0] == {
            "name": "tmux", "status": "ok", "detail": "on PATH", "remediation": None,
        }

    def test_render_ready_and_warning_count(self) -> None:
        r = PrepReport("local-native")
        r.add("a", Status.OK, "fine")
        r.add("b", Status.WARN, "meh")
        out = _prep.render_report(r)
        assert "Ready." in out
        assert "1 warning" in out

    def test_render_not_ready_shows_remediation(self) -> None:
        r = PrepReport("remote-native @ h")
        r.add("tmux", Status.FAIL, "not found", remediation="install tmux")
        out = _prep.render_report(r)
        assert "Not ready" in out
        assert "-> install tmux" in out


class TestGatewayportsCheck:
    """The remote-container GatewayPorts check is OS-aware (macOS vs Linux)."""

    def _check(self, host: str) -> _prep.Check:
        report = PrepReport("remote-container @ h")
        _prep._check_gatewayports(report, host)
        (c,) = [c for c in report.checks if c.name == "gatewayports"]
        return c

    def test_macos_remote_is_ok_advisory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # macOS: gatewayports_status must not even be consulted.
        monkeypatch.setattr(_prep, "remote_uname", lambda host: "Darwin")
        monkeypatch.setattr(
            _prep, "gatewayports_status",
            lambda host: (_ for _ in ()).throw(AssertionError("not consulted on macOS")),
        )
        c = self._check("mac")
        assert c.status is Status.OK
        assert "macOS" in c.detail
        assert c.remediation is None

    def test_linux_disabled_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_prep, "remote_uname", lambda host: "Linux")
        monkeypatch.setattr(_prep, "gatewayports_status", lambda host: "disabled")
        c = self._check("linbox")
        assert c.status is Status.WARN
        assert "Linux" in c.detail
        assert c.remediation is not None and "GatewayPorts clientspecified" in c.remediation

    def test_linux_unknown_warns_with_root_caveat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_prep, "remote_uname", lambda host: "Linux")
        monkeypatch.setattr(_prep, "gatewayports_status", lambda host: "unknown")
        c = self._check("linbox")
        assert c.status is Status.WARN
        assert "sshd -T" in c.detail  # notes the root caveat

    def test_linux_enabled_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_prep, "remote_uname", lambda host: "Linux")
        monkeypatch.setattr(_prep, "gatewayports_status", lambda host: "enabled")
        assert self._check("linbox").status is Status.OK

    def test_unknown_os_treated_as_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An OS the probe could not identify falls to the conservative Linux path.
        monkeypatch.setattr(_prep, "remote_uname", lambda host: "")
        monkeypatch.setattr(_prep, "gatewayports_status", lambda host: "disabled")
        assert self._check("weirdbox").status is Status.WARN


class TestDescribeAndFlags:
    def test_describe_transport(self) -> None:
        assert _prep.describe_transport(LOCAL_NATIVE, "claude") == "local-native (agent: claude)"
        assert (
            _prep.describe_transport(REMOTE_CONTAINER, "codex")
            == "remote-container @ h (agent: codex)"
        )

    def test_prep_flags(self) -> None:
        assert _prep._prep_flags(LOCAL_NATIVE) == ""
        assert _prep._prep_flags(LOCAL_CONTAINER) == " --docker"
        assert _prep._prep_flags(REMOTE_NATIVE) == " --host h"
        assert _prep._prep_flags(REMOTE_CONTAINER) == " --host h --docker"


class TestRefreshTokenExpiry:
    """Parsing claude's refresh-token expiry out of .credentials.json (#2)."""

    def test_reads_refresh_token_expires_at(self) -> None:
        blob = json.dumps({"claudeAiOauth": {
            "accessToken": "secret", "refreshToken": "secret",
            "expiresAt": 1784977227491, "refreshTokenExpiresAt": 1785308018491,
        }})
        assert _prep.refresh_token_expiry(blob) == 1785308018.491

    def test_none_when_field_absent(self) -> None:
        """Older credential files predate the field; that is not a problem."""
        blob = json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": 1}})
        assert _prep.refresh_token_expiry(blob) is None

    def test_none_on_unparseable(self) -> None:
        assert _prep.refresh_token_expiry("not json") is None
        assert _prep.refresh_token_expiry("") is None
        assert _prep.refresh_token_expiry('{"claudeAiOauth": "nope"}') is None
        assert _prep.refresh_token_expiry(
            '{"claudeAiOauth": {"refreshTokenExpiresAt": "soon"}}'
        ) is None


class TestClassifyRefreshExpiry:
    """The access token's own expiry is useless here, the refresh token's is not.

    Verified against the real files: a healthy ~/.claude-docker credential reads
    expiresAt 28.5h in the past (refreshed on use), so an expiresAt check would
    fire on every healthy install. refreshTokenExpiresAt is authoritative: once
    it passes, no refresh can succeed and the session dies on its first turn.
    """

    NOW = 1_000_000.0
    DAY = 86400.0

    def test_ok_when_well_in_future(self) -> None:
        state, _ = _prep.classify_refresh_expiry(self.NOW + 30 * self.DAY, now=self.NOW)
        assert state is _prep.Status.OK

    def test_warns_when_expired(self) -> None:
        state, detail = _prep.classify_refresh_expiry(self.NOW - self.DAY, now=self.NOW)
        assert state is _prep.Status.WARN
        assert "expired" in detail

    def test_warns_when_expiring_within_a_day(self) -> None:
        state, detail = _prep.classify_refresh_expiry(self.NOW + 3600, now=self.NOW)
        assert state is _prep.Status.WARN
        assert "expires" in detail

    def test_ok_when_expiry_unknown(self) -> None:
        """No field, unreadable file: degrade to today's presence-only OK."""
        state, detail = _prep.classify_refresh_expiry(None, now=self.NOW)
        assert state is _prep.Status.OK
        assert "not recorded" in detail
