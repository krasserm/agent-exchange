"""Unit tests for the transport descriptor and command builders."""

from pathlib import Path

import pytest

from agent_session import _transport as T
from agent_session.agents import LaunchSpec, Transport
from agent_session.agents.claude import ClaudeAgent

LOCAL_NATIVE = Transport()
LOCAL_CONTAINER = Transport(runtime="container", container="cc-abcd1234")
REMOTE_NATIVE = Transport(location="remote", host="192.168.94.50")
REMOTE_CONTAINER = Transport(
    location="remote", host="192.168.94.50", runtime="container", container="cc-abcd1234"
)


class TestTransportValidation:
    def test_defaults_are_local_native(self) -> None:
        t = Transport()
        assert t.is_local_native
        assert t.location == "local"
        assert t.runtime == "native"
        t.validate()  # no raise

    def test_remote_requires_host(self) -> None:
        with pytest.raises(ValueError, match="remote transport requires a host"):
            Transport(location="remote").validate()

    def test_local_must_not_set_host(self) -> None:
        with pytest.raises(ValueError, match="local transport must not set a host"):
            Transport(host="h").validate()

    def test_invalid_location_and_runtime(self) -> None:
        with pytest.raises(ValueError, match="location"):
            Transport(location="cloud").validate()
        with pytest.raises(ValueError, match="runtime"):
            Transport(runtime="vm").validate()

    def test_is_local_native_combinations(self) -> None:
        assert LOCAL_NATIVE.is_local_native
        assert not LOCAL_CONTAINER.is_local_native
        assert not REMOTE_NATIVE.is_local_native
        assert not REMOTE_CONTAINER.is_local_native


class TestAttachCommand:
    def test_local_container_uses_docker_exec_with_locale(self) -> None:
        cmd = T.attach_command(LOCAL_CONTAINER, "cc-abcd1234")
        assert cmd.startswith("docker exec -it")
        assert "LANG=C.UTF-8" in cmd
        assert "LC_ALL=C.UTF-8" in cmd
        assert "cc-abcd1234 tmux attach -t cc-abcd1234" in cmd
        assert "ssh" not in cmd  # local, no ssh hop

    def test_remote_native_wraps_ssh_login_shell(self) -> None:
        cmd = T.attach_command(REMOTE_NATIVE, "cc-abcd1234")
        assert cmd.startswith("ssh -t ")
        assert "192.168.94.50" in cmd
        assert "$SHELL -lc" in cmd
        assert "tmux attach -t cc-abcd1234" in cmd
        assert "docker" not in cmd

    def test_remote_container_nests_ssh_and_docker(self) -> None:
        cmd = T.attach_command(REMOTE_CONTAINER, "cc-abcd1234")
        assert cmd.startswith("ssh -t ")
        assert "docker exec -it" in cmd
        assert "tmux attach -t cc-abcd1234" in cmd

    def test_dollar_shell_not_locally_expanded(self) -> None:
        # $SHELL must survive to the remote shell, so it is kept inside single
        # quotes (no local expansion).
        cmd = T.attach_command(REMOTE_NATIVE, "s")
        assert "$SHELL" in cmd


class TestHasSessionCommand:
    def test_no_tty_no_locale(self) -> None:
        cmd = T.has_session_command(LOCAL_CONTAINER, "s")
        assert "tmux has-session -t s" in cmd
        assert "-it" not in cmd
        assert "LANG=" not in cmd  # locale only matters for the interactive TUI

    def test_remote_native(self) -> None:
        cmd = T.has_session_command(REMOTE_NATIVE, "s")
        assert cmd.startswith("ssh ")
        assert not cmd.startswith("ssh -t ")  # ssh allocates no TTY for a probe
        assert "tmux has-session -t s" in cmd


class TestCreateSessionCommand:
    def test_detached_runs_launch_script(self) -> None:
        cmd = T.create_session_command(LOCAL_CONTAINER, "sess", "/home/coder/.agent-session/sessions/k/launch.sh")
        assert "tmux new-session -d -s sess" in cmd
        assert "bash /home/coder/.agent-session/sessions/k/launch.sh" in cmd

    def test_remote_native_create(self) -> None:
        cmd = T.create_session_command(REMOTE_NATIVE, "sess", "/home/u/.agent-session/sessions/k/launch.sh")
        assert cmd.startswith("ssh ")
        assert "new-session -d -s sess" in cmd

    def test_sets_scrollback_friendly_options_on_inner_tmux(self) -> None:
        # Stripping smcup/rmcup keeps the inner tmux client off the outer pane's
        # alternate screen, so scrollback accumulates in the (single) outer pane;
        # status off avoids a nested status bar leaking into that scrollback.
        cmd = T.create_session_command(REMOTE_NATIVE, "sess", "/home/u/k/launch.sh")
        assert "set -g status off" in cmd
        assert "smcup@:rmcup@" in cmd
        assert "terminal-overrides" in cmd


class TestCaptureCommand:
    def test_bounded_captures_last_n_lines_of_inner_tmux(self) -> None:
        cmd = T.capture_command(LOCAL_CONTAINER, "sess", lines=200)
        assert "docker exec" in cmd
        assert "-it" not in cmd  # a capture needs no tty
        assert "tmux capture-pane -p -t sess -S -200" in cmd

    def test_full_history_uses_start_of_history(self) -> None:
        cmd = T.capture_command(LOCAL_CONTAINER, "sess", lines=None)
        assert "tmux capture-pane -p -t sess -S -" in cmd
        assert "-S -200" not in cmd

    def test_remote_native_wraps_ssh(self) -> None:
        cmd = T.capture_command(REMOTE_NATIVE, "sess", lines=None)
        assert cmd.startswith("ssh ")
        assert not cmd.startswith("ssh -t ")  # no tty for a capture
        assert "$SHELL -lc" in cmd
        assert "tmux capture-pane -p -t sess -S -" in cmd

    def test_remote_container_nests_ssh_and_docker(self) -> None:
        cmd = T.capture_command(REMOTE_CONTAINER, "sess", lines=50)
        assert cmd.startswith("ssh ")
        assert "docker exec" in cmd
        assert "tmux capture-pane -p -t sess -S -50" in cmd


class TestTeardownCommands:
    def test_container_removes_container(self) -> None:
        cmds = T.teardown_commands(LOCAL_CONTAINER, "sess", remove_container=True)
        assert len(cmds) == 1
        assert "docker rm -f cc-abcd1234" in cmds[0]

    def test_remote_container_ssh_wraps_rm(self) -> None:
        cmds = T.teardown_commands(REMOTE_CONTAINER, "sess", remove_container=True)
        assert cmds[0].startswith("ssh ")
        assert "docker rm -f cc-abcd1234" in cmds[0]

    def test_native_kills_session(self) -> None:
        cmds = T.teardown_commands(REMOTE_NATIVE, "sess", remove_container=False)
        assert "tmux kill-session -t sess" in cmds[0]
        assert "docker" not in cmds[0]

    def test_container_not_removed_when_flag_false(self) -> None:
        cmds = T.teardown_commands(LOCAL_CONTAINER, "sess", remove_container=False)
        assert "tmux kill-session -t sess" in cmds[0]


class TestClientsCommand:
    def test_local_container(self) -> None:
        cmd = T.clients_command(LOCAL_CONTAINER, "sess")
        assert "tmux list-clients -t sess" in cmd
        assert "docker exec" in cmd

    def test_remote_native(self) -> None:
        cmd = T.clients_command(REMOTE_NATIVE, "sess")
        assert cmd.startswith("ssh ")
        assert "tmux list-clients -t sess" in cmd


class TestReconnectScript:
    def test_loop_structure(self) -> None:
        s = T.render_reconnect_script(REMOTE_NATIVE, "sess", backoff=3.0)
        assert s.startswith("#!/bin/bash")
        assert "while true; do" in s
        assert "backoff=3.0" in s
        # The attach and the has-session probe both appear.
        assert "tmux attach -t sess" in s
        assert "tmux has-session -t sess" in s
        # Only an SSH transport failure (255) keeps retrying after a failed probe.
        assert '"$rc" = 255' in s
        assert "exit 0" in s

    def test_container_script_uses_docker(self) -> None:
        s = T.render_reconnect_script(LOCAL_CONTAINER, "sess")
        assert "docker exec -it" in s


class TestReverseTunnel:
    """``-R`` reverse-tunnel construction for the message-plane read endpoint."""

    def test_native_binds_loopback(self) -> None:
        flags = T.reverse_tunnel_flags(REMOTE_NATIVE, "abcd0000")
        joined = " ".join(flags)
        rport = T.session_rport("abcd0000")
        assert f"-R 127.0.0.1:{rport}:127.0.0.1:{T.READ_PORT}" in joined
        assert "ExitOnForwardFailure=yes" in joined

    def test_container_binds_all_interfaces(self) -> None:
        # remote-container needs a non-loopback bind (GatewayPorts) so the
        # container can reach it via its host gateway.
        flags = T.reverse_tunnel_flags(REMOTE_CONTAINER, "abcd0000")
        rport = T.session_rport("abcd0000")
        assert f"-R 0.0.0.0:{rport}:127.0.0.1:{T.READ_PORT}" in " ".join(flags)

    def test_rport_is_per_session_and_deterministic(self) -> None:
        a = T.session_rport("0001ffff")
        b = T.session_rport("0002ffff")
        assert a != b
        assert a == T.session_rport("0001ffff")  # stable
        assert T.RPORT_BASE <= a < T.RPORT_BASE + T.RPORT_SPAN

    def test_attach_includes_tunnel_only_with_session_key(self) -> None:
        without = T.attach_command(REMOTE_NATIVE, "sess")
        assert "-R " not in without
        withk = T.attach_command(REMOTE_NATIVE, "sess", session_key="abcd0000")
        assert "-R 127.0.0.1:" in withk
        assert "ExitOnForwardFailure=yes" in withk

    def test_local_native_never_tunnels(self) -> None:
        # A local-native session uses the real asn; no ssh, no tunnel.
        cmd = T.attach_command(Transport(), "sess", session_key="abcd0000")
        assert "-R " not in cmd
        assert "ssh" not in cmd

    def test_reconnect_script_threads_tunnel(self) -> None:
        s = T.render_reconnect_script(REMOTE_NATIVE, "sess", session_key="abcd0000")
        rport = T.session_rport("abcd0000")
        assert f"-R 127.0.0.1:{rport}:127.0.0.1:{T.READ_PORT}" in s


class TestReadEndpointAddr:
    def test_remote_native_loopback_rport(self) -> None:
        addr = T.read_endpoint_addr(REMOTE_NATIVE, "abcd0000")
        assert addr == f"127.0.0.1:{T.session_rport('abcd0000')}"

    def test_remote_container_host_gateway_rport(self) -> None:
        addr = T.read_endpoint_addr(REMOTE_CONTAINER, "abcd0000")
        assert addr == f"host.docker.internal:{T.session_rport('abcd0000')}"

    def test_local_container_host_gateway_cport(self) -> None:
        addr = T.read_endpoint_addr(LOCAL_CONTAINER, "abcd0000")
        assert addr == f"host.docker.internal:{T.READ_PORT}"


class TestParameterizedPluginDir:
    """build_launch_command must honor an agent-host plugin dir."""

    def test_claude_plugin_dir_override(self) -> None:
        agent = ClaudeAgent()
        cmd = agent.build_launch_command(
            LaunchSpec(start_dir=Path("/home/coder/workspace")),
            session_key="KEY",
            session_dir=Path("/tmp/sd"),
            plugin_dir=Path("/home/coder/.agent-session/plugin"),
        )
        assert "--plugin-dir /home/coder/.agent-session/plugin/claude" in cmd

    def test_claude_plugin_dir_default(self) -> None:
        agent = ClaudeAgent()
        cmd = agent.build_launch_command(
            LaunchSpec(start_dir=Path("/x")),
            session_key="KEY",
            session_dir=Path("/tmp/sd"),
        )
        assert cmd.rstrip().endswith("plugin/claude")
