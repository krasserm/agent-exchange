"""Shell-out shape tests for the prep engine (subprocess mocked, no real I/O)."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from types import SimpleNamespace

import pytest

from agent_session import _prep
from agent_session._prep import BuildFacts, Check, PrepReport, Status
from agent_session.agents import Transport

MAC = BuildFacts(uname="Darwin", uid=1000, gid=1000)
LINUX = BuildFacts(uname="Linux", uid=1001, gid=1002)

LOCAL_CONTAINER = Transport(runtime="container", container="cc-x")
REMOTE_CONTAINER = Transport(location="remote", host="linbox", runtime="container", container="cc-x")


def _ok(stdout: bytes = b"") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")


def _docker_argv(cmd: list[str]) -> list[str]:
    """The ``docker ...`` argv inside a probe command, local or ssh-wrapped.

    A local probe is already a docker argv. A remote one is
    ``["ssh", ..., host, "$SHELL -lc '<PATH=... docker ...>'"]``, so the docker
    words sit two quoting levels down. Returns ``[]`` for anything else.
    """
    if cmd and cmd[0] == "docker":
        return cmd
    tokens = shlex.split(cmd[-1]) if cmd else []
    if "docker" not in tokens and tokens:
        tokens = shlex.split(tokens[-1])
    return tokens[tokens.index("docker"):] if "docker" in tokens else []


class TestBuildImageIO:
    def test_local_build_streams_dockerfile_on_stdin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict] = []

        def fake_run(cmd, **kw):
            calls.append({"cmd": cmd, "kw": kw})
            return _ok()

        monkeypatch.setattr(_prep.subprocess, "run", fake_run)
        _prep.build_image(
            LOCAL_CONTAINER, "agent-docker",
            facts=MAC, df_bytes=b"FROM node:22\n", ctx_hash="deadbeef", rebuild=False,
        )
        assert len(calls) == 1
        cmd = calls[0]["cmd"]
        assert cmd[:4] == ["docker", "build", "-t", "agent-docker"]
        assert cmd[-1] == "-"  # empty context, Dockerfile on stdin
        assert calls[0]["kw"]["input"] == b"FROM node:22\n"

    def test_remote_build_wraps_in_ssh_with_stdin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict] = []

        def fake_run(cmd, **kw):
            calls.append({"cmd": cmd, "kw": kw})
            return _ok()

        monkeypatch.setattr(_prep.subprocess, "run", fake_run)
        _prep.build_image(
            REMOTE_CONTAINER, "agent-docker",
            facts=LINUX, df_bytes=b"FROM node:22\n", ctx_hash="cafe", rebuild=False,
        )
        cmd = calls[0]["cmd"]
        assert cmd[0] == "ssh"
        assert cmd[1] == "linbox"
        # login shell wraps a docker build that reads the Dockerfile from stdin
        assert "$SHELL -lc" in cmd[2]
        assert "docker build" in cmd[2]
        assert cmd[2].rstrip().endswith("-'")
        # UID/GID build args (Linux host) survive quoting
        assert "UID=1001" in cmd[2]
        # the Dockerfile is forwarded to the remote docker via ssh stdin
        assert calls[0]["kw"]["input"] == b"FROM node:22\n"

    def test_build_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_prep.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
        with pytest.raises(_prep.ProvisionError, match="image build failed"):
            _prep.build_image(
                LOCAL_CONTAINER, "agent-docker",
                facts=MAC, df_bytes=b"x", ctx_hash="h", rebuild=False,
            )


class TestPrepImageCheckOnly:
    """`--check` must never mutate: no build call, whatever the image state."""

    def _stub_no_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_prep, "resolve_build_facts", lambda t: MAC)
        monkeypatch.setattr(_prep, "dockerfile_bytes", lambda: b"FROM node\n")

        def _no_build(*a, **k):
            raise AssertionError("build_image must not be called in --check mode")

        monkeypatch.setattr(_prep, "build_image", _no_build)

    def test_check_only_reports_fail_when_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_no_build(monkeypatch)
        monkeypatch.setattr(_prep, "image_context_hash", lambda img, host: None)
        report = PrepReport("remote-container @ h")
        _prep._prep_image(report, REMOTE_CONTAINER, "agent-docker", "h", check_only=True, rebuild=False)
        (c,) = [c for c in report.checks if c.name == "image"]
        assert c.status is Status.FAIL
        assert "asn prep --host linbox --docker" in c.remediation

    def test_check_only_reports_ok_when_up_to_date(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_no_build(monkeypatch)
        want = _prep.context_hash(b"FROM node\n", MAC)
        monkeypatch.setattr(_prep, "image_context_hash", lambda img, host: want)
        report = PrepReport("local-container")
        _prep._prep_image(report, LOCAL_CONTAINER, "agent-docker", None, check_only=True, rebuild=False)
        (c,) = [c for c in report.checks if c.name == "image"]
        assert c.status is Status.OK

    def test_prep_builds_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_prep, "resolve_build_facts", lambda t: MAC)
        monkeypatch.setattr(_prep, "dockerfile_bytes", lambda: b"FROM node\n")
        monkeypatch.setattr(_prep, "image_context_hash", lambda img, host: None)
        built: list[bool] = []
        monkeypatch.setattr(_prep, "build_image", lambda *a, **k: built.append(True))
        report = PrepReport("local-container")
        _prep._prep_image(report, LOCAL_CONTAINER, "agent-docker", None, check_only=False, rebuild=False)
        assert built == [True]
        (c,) = [c for c in report.checks if c.name == "image"]
        assert c.status is Status.FIXED
        assert "built" in c.detail


class TestProbeShapes:
    def test_docker_ok_local_uses_docker_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[list[str]] = []
        monkeypatch.setattr(_prep.subprocess, "run",
                            lambda cmd, **k: seen.append(cmd) or _ok())
        assert _prep.docker_ok(None) is True
        assert seen[0] == ["docker", "info"]

    def test_docker_ok_remote_uses_ssh_login_shell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[list[str]] = []
        monkeypatch.setattr(_prep.subprocess, "run",
                            lambda cmd, **k: seen.append(cmd) or _ok())
        assert _prep.docker_ok("linbox") is True
        # _remote_login: ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        #                 host, "$SHELL -lc '<snippet>'"]
        assert seen[0][0] == "ssh"
        assert seen[0][5] == "linbox"
        assert "docker info" in seen[0][6]

    def test_image_context_hash_none_on_no_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # docker prints "<no value>" for an unlabeled image -> treat as missing.
        monkeypatch.setattr(_prep.subprocess, "run", lambda *a, **k: _ok(b"<no value>\n"))
        assert _prep.image_context_hash("agent-docker", None) is None

    def test_image_context_hash_returns_label(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_prep.subprocess, "run", lambda *a, **k: _ok(b"abc123\n"))
        assert _prep.image_context_hash("agent-docker", None) == "abc123"

    def test_remote_uname_returns_os(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[list[str]] = []
        monkeypatch.setattr(_prep.subprocess, "run",
                            lambda cmd, **k: seen.append(cmd) or _ok(b"Darwin\n"))
        assert _prep.remote_uname("mac") == "Darwin"
        assert seen[0][0] == "ssh" and seen[0][5] == "mac"
        assert "uname" in seen[0][6]

    def test_remote_uname_empty_on_probe_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_prep.subprocess, "run",
                            lambda *a, **k: SimpleNamespace(returncode=255, stdout=b"", stderr=b""))
        assert _prep.remote_uname("unreachable") == ""


class TestContainerdImageStore:
    """Short image names must resolve on docker's containerd image store.

    Observed on docker 29.2.0 with ``io.containerd.snapshotter.v1`` (both
    machines here): ``docker image inspect agent-docker`` and
    ``... agent-docker:latest`` fail with "No such image" while
    ``docker.io/library/agent-docker:latest`` succeeds, so ``inspect`` is not
    applying the implicit registry/tag normalization for short names. ``docker
    image ls -q``, ``--filter reference=``, and ``docker run`` all resolve the
    same short name fine -- and using the image (a ``run`` or a ``tag``)
    materializes the missing index entry, which is why this only bites until the
    first successful use: exactly the window ``asn prep`` and the container
    preflight look through.

    The fake below reproduces that daemon: short refs resolve through the
    listing, never through ``inspect``. Image IDs always resolve.
    """

    IMAGE_ID = "656dc94fcf03"
    HASH = "7e1bf6266cd6"

    def _containerd_docker(self, monkeypatch: pytest.MonkeyPatch, *, present: bool = True):
        seen: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
            seen.append(cmd)
            match _docker_argv(cmd):
                case ["docker", "image", "ls", "-q", ref]:
                    hit = present and not ref.startswith("absent")
                    return _ok(f"{self.IMAGE_ID}\n".encode() if hit else b"")
                case ["docker", "image", "inspect", *rest] if rest and rest[-1] == self.IMAGE_ID:
                    return _ok(f"{self.HASH}\n".encode())
                case ["docker", "image", "inspect", *_]:
                    # The containerd-store gap: a short name is not resolved.
                    return SimpleNamespace(
                        returncode=1, stdout=b"",
                        stderr=b'Error response from daemon: {"message":"No such image: agent-docker"}\n',
                    )
                case _:
                    return _ok()

        monkeypatch.setattr(_prep.subprocess, "run", fake_run)
        return seen

    def test_image_present_finds_short_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._containerd_docker(monkeypatch)
        assert _prep.image_present("agent-docker", None) is True

    def test_image_absent_is_still_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._containerd_docker(monkeypatch)
        assert _prep.image_present("absent-image", None) is False

    def test_context_hash_reads_label_via_short_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise every ``asn prep`` rebuilds: no label read -> "missing"."""
        self._containerd_docker(monkeypatch)
        assert _prep.image_context_hash("agent-docker", None) == self.HASH
        assert _prep.image_state(_prep.image_context_hash("agent-docker", None), self.HASH) == (
            "up-to-date"
        )

    def test_context_hash_none_when_image_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._containerd_docker(monkeypatch)
        assert _prep.image_context_hash("absent-image", None) is None

    def test_remote_probe_goes_through_the_login_shell(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._containerd_docker(monkeypatch)
        assert _prep.image_present("agent-docker", "linbox") is True
        assert seen[0][0] == "ssh" and seen[0][5] == "linbox"
        assert "docker image ls -q agent-docker" in seen[0][6]


class TestDiagnoseContainerStartFailure:
    def test_reports_missing_image(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_prep, "docker_ok", lambda host: True)
        monkeypatch.setattr(_prep, "image_present", lambda img, host: False)
        msg = _prep.diagnose_container_start_failure(REMOTE_CONTAINER, "agent-docker")
        assert msg is not None
        assert "asn prep --host linbox --docker" in msg

    def test_reports_docker_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_prep, "docker_ok", lambda host: False)
        msg = _prep.diagnose_container_start_failure(LOCAL_CONTAINER, "agent-docker")
        assert msg is not None
        assert "docker is not reachable" in msg

    def test_none_when_image_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_prep, "docker_ok", lambda host: True)
        monkeypatch.setattr(_prep, "image_present", lambda img, host: True)
        assert _prep.diagnose_container_start_failure(LOCAL_CONTAINER, "agent-docker") is None


class TestAuthCheck:
    """`asn prep` must surface a dead credential, not just a present file (#2)."""

    @staticmethod
    def _cred(refresh_expires_at_ms: int | None) -> str:
        oauth: dict = {"accessToken": "x", "refreshToken": "x", "expiresAt": 1}
        if refresh_expires_at_ms is not None:
            oauth["refreshTokenExpiresAt"] = refresh_expires_at_ms
        return json.dumps({"claudeAiOauth": oauth})

    def _run(self, monkeypatch, *, agent: str, blob: str | None) -> Check:
        monkeypatch.setattr(_prep, "_file_exists", lambda path, host: blob is not None)
        monkeypatch.setattr(_prep, "_read_file", lambda path, host: blob)
        report = PrepReport("test")
        _prep._check_auth(report, agent, None, None, container=True)
        return report.checks[0]

    def test_expired_refresh_token_warns_with_remediation(self, monkeypatch) -> None:
        past = int((time.time() - 2 * 86400) * 1000)
        check = self._run(monkeypatch, agent="claude", blob=self._cred(past))
        assert check.status is Status.WARN
        assert "expired" in check.detail
        assert check.remediation  # actionable: re-login / reseed

    def test_healthy_refresh_token_is_ok(self, monkeypatch) -> None:
        future = int((time.time() + 20 * 86400) * 1000)
        check = self._run(monkeypatch, agent="claude", blob=self._cred(future))
        assert check.status is Status.OK
        assert "valid for another" in check.detail

    def test_missing_field_degrades_to_ok(self, monkeypatch) -> None:
        check = self._run(monkeypatch, agent="claude", blob=self._cred(None))
        assert check.status is Status.OK

    def test_unreadable_file_degrades_to_ok(self, monkeypatch) -> None:
        """Present but unreadable (permissions, remote cat failure) != broken."""
        monkeypatch.setattr(_prep, "_file_exists", lambda path, host: True)
        monkeypatch.setattr(_prep, "_read_file", lambda path, host: None)
        report = PrepReport("test")
        _prep._check_auth(report, "claude", None, None, container=True)
        assert report.checks[0].status is Status.OK

    def test_codex_says_it_cannot_check_offline(self, monkeypatch) -> None:
        check = self._run(monkeypatch, agent="codex", blob="{}")
        assert check.status is Status.OK
        assert "not checkable offline" in check.detail

    def test_no_probe_reports_unchecked_validity(self, monkeypatch) -> None:
        """The old '(validity not checked)' wording is gone from every probe."""
        monkeypatch.setattr(_prep, "_file_exists", lambda path, host: False)
        report = PrepReport("test")
        for agent in ("claude", "codex"):
            for container in (True, False):
                _prep._check_auth(report, agent, None, None, container=container)
        monkeypatch.setattr(_prep, "_file_exists", lambda path, host: True)
        monkeypatch.setattr(_prep, "_read_file", lambda path, host: self._cred(None))
        for agent in ("claude", "codex"):
            _prep._check_auth(report, agent, None, None, container=True)
        assert not any("validity not checked" in c.detail for c in report.checks)
