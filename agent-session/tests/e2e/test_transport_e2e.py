"""End-to-end tests across the transport matrix (claude).

Parametrized over ``--targets``: local-native | local-container |
remote-native@HOST | remote-container@HOST. Unreachable targets are skipped by
the ``target_preflight`` fixture, so a partial environment runs the subset it
supports. These require real agents + tmux (+ docker/ssh/mutagen per target).

    uv run pytest tests/e2e/test_transport_e2e.py -v \
        --targets local-container,remote-native@192.168.94.50,remote-container@192.168.94.51

Reliability tests that need a real network partition (severing the link to a
remote host) are gated behind ASN_E2E_NETWORK=1 and a manual step; without it
they skip with instructions.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_session import AgentSession, AgentStatus
from agent_session.agents import Transport

from .helpers import (
    CROSS_HOST_A,
    CROSS_HOST_B,
    E2E_BASE,
    capture_pane,
    container_exists,
    kill_local_proxy,
    make_session_pair,
    raw_read_request,
    read_events,
    read_token,
    remote_session_alive,
    shim_exec,
    start_messaging_session,
    wait_for_idle,
    wait_for_status,
)

pytestmark = pytest.mark.e2e


# --- happy path (every reachable target) -----------------------------------


class TestHappyPath:
    async def test_starts_idle(self, target_session: AgentSession) -> None:
        info = await target_session.get_agent_info()
        assert info is not None
        assert info.status in (AgentStatus.IDLE, AgentStatus.WORKING)

    async def test_send_and_receive(self, target_session: AgentSession) -> None:
        await target_session.send_user_message(
            "Reply with exactly the word: pong"
        )
        await wait_for_idle(target_session, timeout=120)
        msgs = await target_session.get_assistant_messages(last=1)
        assert msgs, "no assistant message after a turn (events did not flow)"
        assert "pong" in msgs[0].message.lower()

    async def test_capture_renders_tui(self, target_session: AgentSession) -> None:
        # The pane shows the agent TUI forwarded through the proxy. Box-drawing
        # rendering relies on the C.UTF-8 locale (container) / login shell.
        out = await target_session.capture(lines=40)
        assert isinstance(out, str) and out.strip()

    async def test_events_reach_local_file(self, target_session: AgentSession) -> None:
        # Proves the observe plane: bind mount (local container) and/or Mutagen
        # mirror (remote) made the events visible to the daemon's watched file.
        await wait_for_status(target_session, AgentStatus.IDLE, timeout=90)
        events = read_events(target_session)
        assert any(e["event"] == "SessionStart" for e in events)


# --- teardown reaps remote resources (asn close) ---------------------------


class TestTeardownReaping:
    async def test_stop_reaps_remote_and_container(
        self,
        transport: Transport,
        target_preflight: None,
        project_factory: Callable[[], Path],
        make_target_session: Callable[..., AgentSession],
    ) -> None:
        if transport.is_local_native:
            pytest.skip("local-native has no remote resources to reap")

        session = make_target_session(project_factory(), transport)
        await session.start(timeout=90)
        assert remote_session_alive(session)
        if transport.runtime == "container":
            assert container_exists(session.transport)

        await session.stop()  # == asn close

        assert not remote_session_alive(session), "remote tmux survived stop"
        if transport.runtime == "container":
            assert not container_exists(session.transport), "container survived stop"


# --- local proxy killed directly -> disconnected -> reattach ---------------


class TestLocalKillAndReattach:
    async def test_disconnected_then_reattach(
        self,
        transport: Transport,
        target_preflight: None,
        project_factory: Callable[[], Path],
        make_target_session: Callable[..., AgentSession],
    ) -> None:
        if transport.is_local_native:
            pytest.skip("local-native: killing local tmux is a real end, not a disconnect")

        session = make_target_session(project_factory(), transport)
        await session.start(timeout=90)
        try:
            await wait_for_status(session, AgentStatus.IDLE, timeout=90)

            # Kill the local proxy directly (no asn close). The agent keeps
            # running in its remote/container tmux.
            kill_local_proxy(session)
            assert remote_session_alive(session), "agent died when local tmux was killed"

            info = await session.get_agent_info()
            assert info is not None and info.status == AgentStatus.DISCONNECTED

            # Reattach restores control without remote discovery.
            await session.reattach()
            await wait_for_status(session, AgentStatus.IDLE, timeout=60)
            await session.send_user_message("Reply with exactly: back")
            await wait_for_idle(session, timeout=120)
            msgs = await session.get_assistant_messages(last=1)
            assert msgs and "back" in msgs[0].message.lower()
        finally:
            await session.stop()


# --- agent /exit ends the session cleanly ----------------------------------


class TestAgentExit:
    async def test_exit_ends_session(self, target_session: AgentSession) -> None:
        await wait_for_status(target_session, AgentStatus.IDLE, timeout=90)
        await target_session.send_user_message("/exit")
        # Remote tmux disappears -> reconnect loop confirms gone and the proxy
        # ends -> session reads as ended.
        deadline = asyncio.get_event_loop().time() + 60
        while asyncio.get_event_loop().time() < deadline:
            if await target_session.get_agent_info() is None:
                break
            await asyncio.sleep(1.0)
        assert await target_session.get_agent_info() is None


# --- concurrent sessions on the same host ----------------------------------


class TestConcurrentSameHost:
    async def test_two_sessions_no_event_collision(
        self,
        transport: Transport,
        target_preflight: None,
        project_factory: Callable[[], Path],
        make_target_session: Callable[..., AgentSession],
    ) -> None:
        s1 = make_target_session(project_factory(), transport)
        s2 = make_target_session(project_factory(), transport)
        try:
            await s1.start(timeout=90)
            await s2.start(timeout=90)
            assert s1.session_key != s2.session_key
            # Distinct per-key event files -> no collision.
            assert s1._events_path != s2._events_path
            await wait_for_status(s1, AgentStatus.IDLE, timeout=90)
            await wait_for_status(s2, AgentStatus.IDLE, timeout=90)
        finally:
            await s1.stop()
            await s2.stop()


# --- network partition + auto-reconnect (manual link severing) -------------


# --- cross-transport inter-session messaging (message plane, §4) -----------


def _free_port() -> int:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
async def messaging_server(target_preflight: None, monkeypatch: pytest.MonkeyPatch):
    """An in-process daemon serving the read endpoint, for the messaging tests.

    Deliberately does NOT call ``DaemonServer.run()`` (whose orphan recovery
    would reap the user's real remote sessions). It opens only the read endpoint
    -- on a free port, with ``_transport.READ_PORT`` patched to match so the
    injected ``ASN_READ_ADDR`` and the ``-R`` tunnel target the same port -- and
    starts sessions via ``_handle_start`` (registry + outbox watcher + token).
    """
    from agent_session import _transport as T
    from agent_session._daemon import DaemonServer

    port = _free_port()
    monkeypatch.setattr(T, "READ_PORT", port)
    monkeypatch.setenv("AGENT_SESSION_DEV_ROOT", str(E2E_BASE.parent))

    server = DaemonServer()
    server._read_server = await asyncio.start_server(
        server._handle_read_connection, host=T.READ_BIND, port=port
    )
    try:
        yield server
    finally:
        for k in list(server._registry.all_keys()):
            await server._cancel_outbox_watcher(k)
            try:
                await server._registry.get(k).stop()
            except Exception:
                pass
            server._registry.remove(k)
        server._read_server.close()
        await server._read_server.wait_closed()


class TestInterSessionMessaging:
    """Send + all three reads across every reachable transport (message plane)."""

    async def test_send_delivers_to_peer(
        self,
        messaging_server,
        transport: Transport,
        project_factory: Callable[[], Path],
    ) -> None:
        # outbox -> mirror/bind-mount -> daemon -> B's tmux, across the transport.
        a, b = await make_session_pair(messaging_server, project_factory, transport)
        await asyncio.to_thread(shim_exec, a, "send", b.session_key, "Reply with exactly: pong")
        await wait_for_idle(b, timeout=180)
        msgs = await b.get_assistant_messages(last=1)
        assert msgs, "peer produced no assistant message (send did not arrive)"
        assert "pong" in msgs[0].message.lower()

    async def test_read_peer_messages_over_endpoint(
        self,
        messaging_server,
        transport: Transport,
        project_factory: Callable[[], Path],
    ) -> None:
        # tunnel/gateway + endpoint + token, end to end.
        a, b = await make_session_pair(messaging_server, project_factory, transport)
        await a.send_user_message("Reply with exactly: alpha-reply")
        await wait_for_idle(a, timeout=180)
        out = await asyncio.to_thread(shim_exec, b, "messages", a.session_key)
        assert "alpha-reply" in out.lower()

    async def test_read_peer_status(
        self,
        messaging_server,
        transport: Transport,
        project_factory: Callable[[], Path],
    ) -> None:
        a, b = await make_session_pair(messaging_server, project_factory, transport)
        await wait_for_status(a, AgentStatus.IDLE, timeout=120)
        out = await asyncio.to_thread(shim_exec, b, "status", a.session_key)
        assert out.strip() in {"idle", "working", "waiting"}

    async def test_read_peer_capture(
        self,
        messaging_server,
        transport: Transport,
        project_factory: Callable[[], Path],
    ) -> None:
        a, b = await make_session_pair(messaging_server, project_factory, transport)
        await wait_for_status(a, AgentStatus.IDLE, timeout=120)
        out = await asyncio.to_thread(shim_exec, b, "capture", a.session_key)
        assert out.strip(), "capture returned empty TUI text"

    async def test_token_required(
        self,
        messaging_server,
        transport: Transport,
        project_factory: Callable[[], Path],
    ) -> None:
        a, b = await make_session_pair(messaging_server, project_factory, transport)
        # A wrong token is rejected (shim exits non-zero -> RuntimeError).
        with pytest.raises(RuntimeError):
            await asyncio.to_thread(shim_exec, b, "messages", a.session_key, env_extra={"ASN_READ_TOKEN": "wrong"})
        # The correct (injected) token succeeds.
        await asyncio.to_thread(shim_exec, b, "status", a.session_key)

    async def test_read_method_whitelist(
        self,
        messaging_server,
        transport: Transport,
        project_factory: Callable[[], Path],
    ) -> None:
        # A non-whitelisted method is refused even with a valid token; drive raw
        # line-JSON from the control host (the shim only speaks whitelisted verbs).
        from agent_session import _transport as T

        a = await start_messaging_session(messaging_server, transport, project_factory())
        token = read_token(a)
        refused = await asyncio.to_thread(raw_read_request, 
            T.READ_PORT, {"token": token, "method": "stop", "params": {"key": a.session_key}}
        )
        assert refused["ok"] is False and "not allowed" in refused["error"]
        allowed = await asyncio.to_thread(raw_read_request, 
            T.READ_PORT, {"token": token, "method": "status", "params": {"key": a.session_key}}
        )
        assert allowed["ok"] is True

    async def test_two_sessions_same_host_no_collision(
        self,
        messaging_server,
        transport: Transport,
        project_factory: Callable[[], Path],
    ) -> None:
        # Two sessions on one host: each must reach the read endpoint over its
        # own per-session -R tunnel (one must not starve the other).
        if transport.is_local_native:
            pytest.skip("local-native has no tunnel to collide")
        from agent_session import _transport as T

        a, b = await make_session_pair(messaging_server, project_factory, transport)
        assert T.session_rport(a.session_key) != T.session_rport(b.session_key)

        await a.send_user_message("Reply with exactly: from-a")
        await b.send_user_message("Reply with exactly: from-b")
        await wait_for_idle(a, timeout=180)
        await wait_for_idle(b, timeout=180)

        # Both tunnels live concurrently: each reads the other across its own.
        out_a = await asyncio.to_thread(shim_exec, b, "messages", a.session_key)
        out_b = await asyncio.to_thread(shim_exec, a, "messages", b.session_key)
        assert "from-a" in out_a.lower()
        assert "from-b" in out_b.lower()

    async def test_agent_invokes_shim_send(
        self,
        messaging_server,
        transport: Transport,
        project_factory: Callable[[], Path],
    ) -> None:
        # The realistic path: the agent itself runs `asn send` (on PATH via the
        # injected launch env). Only proxied transports inject that env + shim.
        # The sender runs in bypass mode so the shell command runs unattended
        # (the host's claude need not be in auto-accept mode).
        if transport.is_local_native:
            pytest.skip("local-native does not inject the shim env into the agent")
        a = await start_messaging_session(
            messaging_server, transport, project_factory(), skip_permissions=True
        )
        b = await start_messaging_session(messaging_server, transport, project_factory())
        await a.send_user_message(
            f"Use the Bash tool to run exactly this command and nothing else, "
            f"then stop: asn send {b.session_key} 'Reply with exactly: relayed'"
        )
        await wait_for_idle(a, timeout=180)
        await wait_for_idle(b, timeout=180)
        msgs = await b.get_assistant_messages(last=1)
        assert msgs and "relayed" in msgs[0].message.lower()


# --- cross-host messaging (env-gated; two remote hosts) --------------------


class TestCrossHostMessaging:
    async def test_cross_host_messaging(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if os.environ.get("ASN_E2E_CROSS_HOST") != "1":
            pytest.skip(
                "set ASN_E2E_CROSS_HOST=1 (and ASN_E2E_HOST_A / ASN_E2E_HOST_B) to "
                "verify routing through the single control daemon for sessions on "
                "two different machines"
            )
        import shlex
        import shutil
        import subprocess
        import uuid

        from agent_session import _transport as T
        from agent_session._daemon import DaemonServer
        from agent_session.agents import Transport

        host_a = os.environ.get("ASN_E2E_HOST_A", CROSS_HOST_A)
        host_b = os.environ.get("ASN_E2E_HOST_B", CROSS_HOST_B)
        ta = Transport(location="remote", host=host_a)
        tb = Transport(location="remote", host=host_b)

        port = _free_port()
        monkeypatch.setattr(T, "READ_PORT", port)
        monkeypatch.setenv("AGENT_SESSION_DEV_ROOT", str(E2E_BASE.parent))

        def _remote_project(host: str) -> Path:
            name = f"e2e-{uuid.uuid4().hex[:8]}"
            home = subprocess.run(
                ["ssh", host, 'printf %s "$HOME"'], capture_output=True, text=True, check=True
            ).stdout.strip()
            path = f"{home}/agent-session-e2e/{name}"
            subprocess.run(
                ["ssh", host,
                 f"mkdir -p {shlex.quote(path)} && cd {shlex.quote(path)} && "
                 "git init -q && git commit -q --allow-empty -m init"],
                check=True, capture_output=True,
            )
            return Path(path)

        server = DaemonServer()
        server._read_server = await asyncio.start_server(
            server._handle_read_connection, host=T.READ_BIND, port=port
        )
        try:
            a = await start_messaging_session(server, ta, _remote_project(host_a))
            b = await start_messaging_session(server, tb, _remote_project(host_b))
            # A (on host A) sends to B (on host B), routed through the one daemon.
            await asyncio.to_thread(shim_exec, a, "send", b.session_key, "Reply with exactly: crossed")
            await wait_for_idle(b, timeout=180)
            msgs = await b.get_assistant_messages(last=1)
            assert msgs and "crossed" in msgs[0].message.lower()
            # And A reads B's reply back over B's tunnel.
            out = await asyncio.to_thread(shim_exec, a, "messages", b.session_key)
            assert "crossed" in out.lower()
        finally:
            for k in list(server._registry.all_keys()):
                await server._cancel_outbox_watcher(k)
                try:
                    await server._registry.get(k).stop()
                except Exception:
                    pass
                server._registry.remove(k)
            server._read_server.close()
            await server._read_server.wait_closed()


class TestNetworkPartition:
    async def test_reconnect_after_partition(
        self,
        transport: Transport,
        target_preflight: None,
        project_factory: Callable[[], Path],
        make_target_session: Callable[..., AgentSession],
    ) -> None:
        if transport.location != "remote":
            pytest.skip("network partition only applies to remote targets")
        if os.environ.get("ASN_E2E_NETWORK") != "1":
            pytest.skip(
                "set ASN_E2E_NETWORK=1 and be ready to sever the link to the "
                "remote host when prompted; verifies disconnected->reconnect "
                "with no event loss"
            )

        session = make_target_session(project_factory(), transport)
        await session.start(timeout=90)
        try:
            await wait_for_status(session, AgentStatus.IDLE, timeout=90)
            before = len(read_events(session))

            print(f"\n>>> SEVER the network link to {transport.host} now; "
                  "waiting 20s...")
            await asyncio.sleep(20)
            info = await session.get_agent_info()
            assert info is not None and info.status == AgentStatus.DISCONNECTED

            print(">>> RESTORE the network link now; waiting 30s for reconnect...")
            await asyncio.sleep(30)
            await wait_for_status(session, AgentStatus.IDLE, timeout=60)

            # Durable remote events + Mutagen resync -> nothing lost.
            assert len(read_events(session)) >= before
            await session.send_user_message("Reply with exactly: recovered")
            await wait_for_idle(session, timeout=120)
            msgs = await session.get_assistant_messages(last=1)
            assert msgs and "recovered" in msgs[0].message.lower()
        finally:
            await session.stop()
