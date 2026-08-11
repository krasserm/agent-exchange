"""Unit tests for agent-host path resolution and provisioning helpers."""

import shlex
import subprocess
from pathlib import Path

import pytest

from agent_session import _naming, _provision
from agent_session.agents import Transport
from agent_session.agents.claude import ClaudeAgent
from agent_session.agents.codex import CodexAgent

LOCAL_NATIVE = Transport()
LOCAL_CONTAINER = Transport(runtime="container", container="cc-abcd1234")
REMOTE_NATIVE = Transport(location="remote", host="h")
REMOTE_CONTAINER = Transport(
    location="remote", host="h", runtime="container", container="cc-abcd1234"
)


class TestAgentHostPaths:
    def test_home_native_vs_container(self) -> None:
        assert _provision.agent_host_home(LOCAL_NATIVE, local_home="/Users/m") == "/Users/m"
        assert _provision.agent_host_home(REMOTE_NATIVE, local_home="/remote/u") == "/remote/u"
        assert _provision.agent_host_home(LOCAL_CONTAINER, local_home="/Users/m") == "/home/coder"

    def test_project_container_is_fixed_workspace(self) -> None:
        assert _provision.agent_host_project(LOCAL_CONTAINER, "/anything") == "/home/coder/workspace"
        assert _provision.agent_host_project(REMOTE_NATIVE, "/srv/proj") == "/srv/proj"

    def test_plugin_root(self) -> None:
        assert (
            _provision.agent_host_plugin_root(LOCAL_CONTAINER, local_home="/Users/m")
            == "/home/coder/.agent-session/plugin"
        )
        assert (
            _provision.agent_host_plugin_root(REMOTE_NATIVE, local_home="/u")
            == "/u/.agent-session/plugin"
        )

    def test_session_dir_in_runtime(self) -> None:
        assert (
            _provision.agent_host_session_dir(LOCAL_CONTAINER, "KEY", local_home="/Users/m")
            == "/home/coder/.agent-session/sessions/KEY"
        )
        assert (
            _provision.agent_host_session_dir(REMOTE_NATIVE, "KEY", local_home="/u")
            == "/u/.agent-session/sessions/KEY"
        )

    def test_local_root_is_the_control_host_state_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A local agent host shares the daemon's state root, ``ASN_HOME`` and all.

        The daemon watches ``DAEMON_DIR/sessions/<key>/events.jsonl``. If a local
        host's paths come from ``$HOME`` instead, a local container bind-mounts a
        tree nobody watches and the session dies of a bare readiness timeout.
        """
        monkeypatch.setattr(_naming, "DAEMON_DIR", tmp_path / "state")

        for transport in (LOCAL_NATIVE, LOCAL_CONTAINER):
            assert (
                _provision.host_agent_session_root(transport, remote_home=None)
                == str(tmp_path / "state")
            )
            assert (
                _provision.host_agent_session_dir_on_disk(transport, "KEY", remote_home=None)
                == str(tmp_path / "state") + "/sessions/KEY"
            )

    def test_remote_root_ignores_the_local_state_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The remote side runs no daemon and knows no ASN_HOME; its tree lives
        # under its own home and reaches us through the mirror.
        monkeypatch.setattr(_naming, "DAEMON_DIR", tmp_path / "state")
        assert (
            _provision.host_agent_session_root(REMOTE_CONTAINER, remote_home="/home/u")
            == "/home/u/.agent-session"
        )

    def test_session_dir_on_disk_uses_remote_home_for_container(self) -> None:
        # A remote container's events live in the *host's* ~/.agent-session
        # (bind-mount source), not the in-container path.
        assert (
            _provision.host_agent_session_dir_on_disk(REMOTE_CONTAINER, "KEY", remote_home="/home/u")
            == "/home/u/.agent-session/sessions/KEY"
        )

    def test_session_dir_on_disk_remote_requires_home(self) -> None:
        with pytest.raises(ValueError, match="remote home"):
            _provision.host_agent_session_dir_on_disk(REMOTE_NATIVE, "KEY", remote_home=None)


class TestContainerRunCommand:
    def test_mounts_and_hardening(self) -> None:
        argv = _provision.container_run_command(
            LOCAL_CONTAINER,
            agent=ClaudeAgent(),
            image="agent-docker",
            project_on_host="/Users/m/proj",
            agent_session_on_host="/Users/m/.agent-session",
            config_dir_on_host="/Users/m/.claude-docker",
            gitconfig_on_host="/Users/m/.gitconfig",
        )
        s = " ".join(argv)
        assert "docker run -d --name cc-abcd1234" in s
        # reaches the daemon read endpoint via the host gateway (message plane)
        assert "--add-host host.docker.internal:host-gateway" in s
        assert "LANG=C.UTF-8" in s
        assert "/Users/m/.claude-docker:/home/coder/.claude" in s
        assert "/Users/m/.claude-docker/.claude.json:/home/coder/.claude.json" in s
        # token writable by default (in-container refresh persists), so no ro overlay
        assert ":ro" not in s.replace("/home/coder/.gitconfig:ro", "")
        assert "/Users/m/.agent-session:/home/coder/.agent-session" in s

    def test_codex_mounts_config_dir(self) -> None:
        argv = _provision.container_run_command(
            LOCAL_CONTAINER,
            agent=CodexAgent(),
            image="agent-docker",
            project_on_host="/Users/m/proj",
            agent_session_on_host="/Users/m/.agent-session",
            config_dir_on_host="/Users/m/.codex-docker",
            gitconfig_on_host=None,
        )
        s = " ".join(argv)
        # Codex shares the host login through a single ~/.codex dir mount.
        assert "/Users/m/.codex-docker:/home/coder/.codex" in s
        assert "/home/coder/.claude" not in s
        assert "/Users/m/.agent-session:/home/coder/.agent-session" in s

    def test_credentials_readonly_overlay_opt_in(self) -> None:
        argv = _provision.container_run_command(
            LOCAL_CONTAINER,
            agent=ClaudeAgent(),
            image="agent-docker",
            project_on_host="/p",
            agent_session_on_host="/a",
            config_dir_on_host="/Users/m/.claude-docker",
            gitconfig_on_host=None,
            credentials_readonly=True,
        )
        assert (
            "/Users/m/.claude-docker/.credentials.json:/home/coder/.claude/.credentials.json:ro"
            in " ".join(argv)
        )

    def test_gitconfig_optional(self) -> None:
        argv = _provision.container_run_command(
            LOCAL_CONTAINER,
            agent=ClaudeAgent(),
            image="agent-docker",
            project_on_host="/p",
            agent_session_on_host="/a",
            config_dir_on_host="/c",
            gitconfig_on_host=None,
        )
        assert "gitconfig" not in " ".join(argv)


class TestPluginChecksum:
    def test_checksum_stable_and_sensitive(self, tmp_path: Path) -> None:
        src = tmp_path / "plugin"
        (src / "claude" / ".claude-plugin").mkdir(parents=True)
        (src / "claude" / "hooks").mkdir(parents=True)
        (src / "log-event.sh").write_text("echo hi\n")
        (src / "asn").write_text("echo shim\n")
        (src / "claude" / ".claude-plugin" / "plugin.json").write_text("{}")
        (src / "claude" / "hooks" / "hooks.json").write_text("{}")

        c1 = _provision.plugin_checksum(src)
        c2 = _provision.plugin_checksum(src)
        assert c1 == c2  # stable

        (src / "log-event.sh").write_text("echo changed\n")
        assert _provision.plugin_checksum(src) != c1  # sensitive to content

    def test_real_plugin_checksums(self) -> None:
        # The bundled plugin must have all three files so the checksum works.
        agent_plugin = (
            Path(__import__("agent_session").__file__).parent / "plugin"
        )
        c = _provision.plugin_checksum(agent_plugin)
        assert len(c) == 64  # sha256 hex


class TestContainerMountGuard:
    """docker turns a non-absolute ``-v`` source into a *named volume*.

    The agent then runs in a fresh empty directory and reports success, which is
    the worst possible failure mode. Refuse to build the command instead.
    """

    def _run_command(self, project_on_host: str) -> list[str]:
        return _provision.container_run_command(
            REMOTE_CONTAINER,
            agent=ClaudeAgent(),
            image="img",
            project_on_host=project_on_host,
            agent_session_on_host="/home/u/.agent-session",
            config_dir_on_host="/home/u/.claude-docker",
            gitconfig_on_host=None,
        )

    @pytest.mark.parametrize("bad", ["rel/proj", "~/proj", "proj", "."])
    def test_rejects_non_absolute_project_mount(self, bad: str) -> None:
        with pytest.raises(ValueError, match="absolute"):
            self._run_command(bad)

    def test_absolute_project_mount_is_accepted(self) -> None:
        argv = self._run_command("/srv/proj")
        assert "-v" in argv
        assert f"/srv/proj:{_provision.CONTAINER_WORKSPACE}" in argv


class TestInnerLaunchScriptCdGuard:
    """A failed ``cd`` must stop the launch, not run the agent somewhere else.

    The script has no ``set -e``, so a bare ``cd`` that fails would fall through
    and start the agent in whatever directory the tmux opened in -- a silently
    wrong project. These tests run the generated script for real, with a stub
    agent on ``PATH`` standing in for claude, and check what the script *does*.
    """

    def _script_and_stub(self, tmp_path: Path, project: Path) -> tuple[Path, Path]:
        """Render the launch script for *project*; return (script, marker) paths.

        The stub agent goes in the plugin dir, which the script puts first on
        ``PATH``, so it wins over any real claude on this machine. It records the
        directory it was launched from, which is the thing under test.
        """
        from agent_session.agents import LaunchSpec

        home = tmp_path / "home"
        plugin = home / ".agent-session" / "plugin"
        plugin.mkdir(parents=True)
        marker = tmp_path / "launched-from"
        stub = plugin / "claude"
        stub.write_text(f'#!/bin/bash\npwd > {shlex.quote(str(marker))}\n')
        stub.chmod(0o755)

        script = _provision.build_inner_launch_script(
            ClaudeAgent(),
            LaunchSpec(start_dir=project, transport=REMOTE_NATIVE),
            session_key="K",
            session_dir=Path("/tmp/sd"),
            transport=REMOTE_NATIVE,
            local_home=str(home),
            read_addr="127.0.0.1:1",
            read_token="t",
        )
        script_path = tmp_path / "launch.sh"
        script_path.write_text(script)
        return script_path, marker

    def test_missing_project_dir_aborts_before_the_agent_runs(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "not-there"
        script_path, marker = self._script_and_stub(tmp_path, missing)

        proc = subprocess.run(
            ["bash", str(script_path)], capture_output=True, text=True
        )

        assert proc.returncode != 0, "a failed cd must fail the launch"
        assert not marker.exists(), (
            f"agent was launched anyway, from {marker.read_text().strip() if marker.exists() else '?'}"
        )
        # Breadcrumb for a hand-run of the script; the window is gone by the time
        # anyone could read it from the pane (see the comment in _provision).
        assert str(missing) in proc.stderr

    def test_existing_project_dir_launches_the_agent_there(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        script_path, marker = self._script_and_stub(tmp_path, project)

        proc = subprocess.run(
            ["bash", str(script_path)], capture_output=True, text=True
        )

        assert proc.returncode == 0, proc.stderr
        assert Path(marker.read_text().strip()) == project.resolve()


class TestInnerLaunchScript:
    def test_exports_key_and_cds_to_project(self) -> None:
        from agent_session.agents.claude import ClaudeAgent
        from agent_session.agents import LaunchSpec

        script = _provision.build_inner_launch_script(
            ClaudeAgent(),
            LaunchSpec(start_dir=Path("/srv/proj"), transport=REMOTE_NATIVE),
            session_key="KEY123",
            session_dir=Path("/tmp/sd"),
            transport=REMOTE_NATIVE,
            local_home="/u",
            read_addr="127.0.0.1:48123",
            read_token="tok-abc",
        )
        assert "export AGENT_SESSION_KEY=KEY123" in script
        assert "cd /srv/proj" in script
        assert "--plugin-dir /u/.agent-session/plugin/claude" in script
        # message-plane env + the shim's plugin dir on PATH
        assert "export ASN_READ_ADDR=127.0.0.1:48123" in script
        assert "export ASN_READ_TOKEN=tok-abc" in script
        assert "export PATH=/u/.agent-session/plugin:" in script
        # A polluted tmux/ssh environment must not leak a foreign venv into
        # the agent (uv warns on VIRTUAL_ENV mismatch).
        assert "unset VIRTUAL_ENV VIRTUAL_ENV_PROMPT\n" in script

    def test_container_uses_workspace_and_container_plugin(self) -> None:
        from agent_session.agents.claude import ClaudeAgent
        from agent_session.agents import LaunchSpec

        script = _provision.build_inner_launch_script(
            ClaudeAgent(),
            LaunchSpec(start_dir=Path("/Users/m/proj"), transport=LOCAL_CONTAINER),
            session_key="K",
            session_dir=Path("/tmp/sd"),
            transport=LOCAL_CONTAINER,
            local_home="/Users/m",
            read_addr="host.docker.internal:47600",
            read_token="tok-k",
        )
        assert "cd /home/coder/workspace" in script
        assert "--plugin-dir /home/coder/.agent-session/plugin/claude" in script
        assert "export ASN_READ_ADDR=host.docker.internal:47600" in script
        assert "export PATH=/home/coder/.agent-session/plugin:" in script
        # Claude's Bash tool inherits the exported PATH, so no shim link is needed.
        assert ".local/bin/asn" not in script

    def test_codex_container_links_shim_onto_login_path(self) -> None:
        from agent_session.agents import LaunchSpec

        script = _provision.build_inner_launch_script(
            CodexAgent(),
            LaunchSpec(start_dir=Path("/Users/m/proj"), transport=LOCAL_CONTAINER),
            session_key="K",
            session_dir=Path("/tmp/sd"),
            transport=LOCAL_CONTAINER,
            local_home="/Users/m",
            read_addr="host.docker.internal:47600",
            read_token="tok-k",
        )
        # Codex's shell tool runs a login shell that resets PATH from the image
        # profile, dropping the exported plugin dir; ~/.local/bin survives the
        # reset, so the `asn` shim is linked there before codex launches.
        assert (
            'ln -sf /home/coder/.agent-session/plugin/asn "$HOME/.local/bin/asn"'
            in script
        )
        # The link must precede the agent command so `asn` resolves on first use.
        assert script.index(".local/bin/asn") < script.index("codex")

    def test_codex_native_needs_no_shim_link(self) -> None:
        from agent_session.agents import LaunchSpec

        script = _provision.build_inner_launch_script(
            CodexAgent(),
            LaunchSpec(start_dir=Path("/srv/proj"), transport=REMOTE_NATIVE),
            session_key="K",
            session_dir=Path("/tmp/sd"),
            transport=REMOTE_NATIVE,
            local_home="/u",
            read_addr="127.0.0.1:48123",
            read_token="tok-k",
        )
        # Native profiles append to PATH, so the exported plugin dir survives and
        # no shim link is emitted.
        assert ".local/bin/asn" not in script
