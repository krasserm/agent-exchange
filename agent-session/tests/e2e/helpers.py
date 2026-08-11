"""Shared helpers for e2e tests."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import libtmux

from agent_session import AgentInfo, AgentSession, AgentStatus, _prep, _provision
from agent_session._prep import PATH_PREFIX as _PATH_PREFIX
from agent_session.agents import Transport

# Dev root for e2e tests -- uses a temp dir under ~/Development.
E2E_BASE = Path("~/Development/.agent-session-e2e").expanduser()

# Transport targets the e2e suite can run against. Format:
#   local-native | local-container | remote-native@HOST | remote-container@HOST
DEFAULT_TARGETS = "local-native"

# Hosts the cross-host messaging test pairs when ASN_E2E_CROSS_HOST=1 and
# ASN_E2E_HOST_A / ASN_E2E_HOST_B are not set. Shared with the mirror-teardown
# fixture, which has to know every host a run may have built a mirror for.
CROSS_HOST_A = "192.168.94.50"
CROSS_HOST_B = "192.168.94.51"


def parse_target(spec: str) -> Transport:
    """Parse a ``--targets`` token into a :class:`Transport`."""
    name, _, host = spec.partition("@")
    match name:
        case "local-native":
            return Transport()
        case "local-container":
            return Transport(runtime="container")
        case "remote-native":
            if not host:
                raise ValueError(f"target {spec!r} needs @HOST")
            return Transport(location="remote", host=host)
        case "remote-container":
            if not host:
                raise ValueError(f"target {spec!r} needs @HOST")
            return Transport(location="remote", host=host, runtime="container")
        case _:
            raise ValueError(f"unknown target: {spec!r}")


def target_skip_reason(transport: Transport, image: str = "agent-docker") -> str | None:
    """Return why *transport* can't run here, or None if its prereqs are met.

    Used by the preflight fixture to *skip* (not fail) targets whose machine,
    docker, image, mutagen, or native claude is unavailable -- so a partial
    environment runs the subset it supports instead of failing at readiness.
    The probes are the production ones from ``_prep`` (single source of truth).
    """
    host = transport.host if transport.location == "remote" else None
    if transport.location == "remote":
        if not _prep.ssh_ok(transport.host):
            return f"remote host {transport.host} not reachable over ssh"
        if not _prep.mutagen_ok():
            return "mutagen not installed on the control host"
    if transport.runtime == "container":
        if not _prep.docker_ok(host):
            return f"docker not available on {host or 'local'}"
        if not _prep.image_present(image, host):
            return f"image '{image}' not present on {host or 'local'} (build it first)"
    else:  # native
        if not _prep.agent_cli_ok("claude", host):
            return f"claude not installed on {host or 'local'}"
    return None


async def wait_for_status(
    session: AgentSession,
    status: AgentStatus,
    timeout: float = 60,
) -> AgentInfo:
    """Poll get_agent_info() until the expected status is reached."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        info = await session.get_agent_info()
        if info is not None and info.status == status:
            return info
        await asyncio.sleep(0.5)
    raise TimeoutError(
        f"Status {status} not reached within {timeout}s "
        f"(current: {await session.get_agent_info()})"
    )


async def wait_for_idle(session: AgentSession, timeout: float = 60) -> AgentInfo:
    """Wait for the session to become idle.

    If currently idle, waits for a non-idle status first (to handle the
    common pattern of sending a message and waiting for the response).
    """
    info = await session.get_agent_info()
    if info is not None and info.status == AgentStatus.IDLE:
        # Wait for a non-idle status first (WORKING or WAITING).
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            info = await session.get_agent_info()
            if info is not None and info.status != AgentStatus.IDLE:
                break
            if info is None:
                break  # Session ended.
            await asyncio.sleep(0.5)
    return await wait_for_status(session, AgentStatus.IDLE, timeout)


async def wait_for_info_none(session: AgentSession, timeout: float = 30) -> None:
    """Wait for get_agent_info() to return None (session ended)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await session.get_agent_info() is None:
            return
        await asyncio.sleep(0.5)
    raise TimeoutError(f"Session did not end within {timeout}s")


async def active_info(session: AgentSession, timeout: float = 120) -> AgentInfo:
    """Return info for a session that has assigned a session id.

    Agents like codex assign a session id (and emit status) only once the
    first turn begins. This primes such a session with a trivial prompt so
    callers can rely on ``session_id`` being populated.
    """
    info = await session.get_agent_info()
    if info is not None and info.session_id is not None:
        return info
    await session.send_user_message("Reply with exactly: ready")
    await wait_for_idle(session, timeout=timeout)
    info = await session.get_agent_info()
    assert info is not None and info.session_id is not None
    return info


def send_via_tmux(session_name: str, text: str) -> None:
    """Send text directly to tmux pane, simulating native terminal UI input."""
    import time

    server = libtmux.Server()
    session = server.sessions.get(session_name=session_name, default=None)
    assert session is not None, f"tmux session '{session_name}' not found"
    pane = session.active_pane
    assert pane is not None
    pane.send_keys(text, enter=False)
    time.sleep(0.5)
    pane.send_keys("", enter=True)


def capture_pane(session_name: str, lines: int = 50) -> str:
    """Capture recent terminal output from the tmux pane."""
    server = libtmux.Server()
    session = server.sessions.get(session_name=session_name, default=None)
    assert session is not None, f"tmux session '{session_name}' not found"
    pane = session.active_pane
    assert pane is not None
    output = pane.capture_pane(start=-lines)
    return "\n".join(output)


def read_events(session: AgentSession) -> list[dict]:
    """Read all events from the session's events file (local dir or mirror)."""
    events_path = session._events_path
    if not events_path.exists():
        return []
    events = []
    for line in events_path.read_text().strip().splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


async def wait_for_event_logged(
    session: AgentSession, event_name: str, timeout: float = 15,
) -> bool:
    """Poll the events file until *event_name* appears, or timeout.

    Some events land slightly after the turn goes idle (e.g. claude emits
    SubagentStop ~2s after Stop), so reading once right after ``wait_for_idle``
    races them. Returns True if seen within *timeout*.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if any(e["event"] == event_name for e in read_events(session)):
            return True
        await asyncio.sleep(0.5)
    return False


def remote_session_alive(session: AgentSession) -> bool:
    """True iff the agent's tmux session still exists on the agent host."""
    from agent_session import _transport as T

    assert session.remote_session_name is not None
    cmd = T.has_session_command(session.transport, session.remote_session_name)
    return subprocess.run(["bash", "-c", cmd], capture_output=True).returncode == 0


def container_exists(transport: Transport) -> bool:
    """True iff the session's container exists on its host."""
    import shlex
    inspect = ["docker", "inspect", transport.container]
    if transport.location == "remote":
        cmd = ["ssh", transport.host, f"$SHELL -lc {shlex.quote(shlex.join(inspect))}"]
    else:
        cmd = inspect
    return subprocess.run(cmd, capture_output=True).returncode == 0


def kill_local_proxy(session: AgentSession) -> None:
    """Kill the local proxy tmux directly (simulating a non-asn teardown)."""
    libtmux.Server().kill_session(session.session_name)


# --- message-plane helpers (TestInterSessionMessaging) ----------------------


async def start_messaging_session(
    server,
    transport: Transport,
    project_dir: Path,
    *,
    timeout: float = 90,
    skip_permissions: bool = False,
) -> AgentSession:
    """Start a claude session *through the daemon* and return its live object.

    Going through ``_handle_start`` (not a bare ``AgentSession.start``) is what
    registers the session, persists its read token, and arms its outbox watcher
    -- the daemon-owned machinery the message plane needs. The returned object is
    the one the daemon holds, so reads/sends routed through the daemon reach it.

    ``skip_permissions`` runs claude in bypass mode so it can run a shell command
    (e.g. invoke the ``asn`` shim itself) without a per-command approval prompt --
    needed where the host's claude is not in auto-accept mode (container managed
    settings only apply inside the container).
    """
    params: dict = {"agent": "claude", "start_dir": str(project_dir), "timeout": timeout}
    if skip_permissions:
        params["skip_permissions"] = True
    if transport.location == "remote":
        params["location"] = "remote"
        params["host"] = transport.host
    if transport.runtime == "container":
        params["runtime"] = "container"
    result = await server._handle_start(params)
    return server._registry.get(result["session_key"])


async def make_session_pair(
    server,
    project_factory: Callable[[], Path],
    transport: Transport,
    *,
    timeout: float = 90,
) -> tuple[AgentSession, AgentSession]:
    """Start two claude sessions on the same transport, registered in *server*."""
    a = await start_messaging_session(server, transport, project_factory(), timeout=timeout)
    b = await start_messaging_session(server, transport, project_factory(), timeout=timeout)
    return a, b


def _shim_path(session: AgentSession) -> str:
    """Path to the deployed ``asn`` shim on the session's agent host.

    Proxied transports deploy it via ``ensure_plugin``; local-native does not
    (it uses the real ``asn``), so point at the bundled copy -- the same file --
    to exercise the shim uniformly.
    """
    import agent_session

    t = session.transport
    if t.is_local_native:
        return str(Path(agent_session.__file__).parent / "plugin" / "asn")
    home = session._remote_home if t.location == "remote" else str(Path.home())
    return f"{_provision.agent_host_plugin_root(t, local_home=home)}/asn"


def _shim_inner(asn_path: str, env: dict[str, str], argv: tuple[str, ...], *, with_path: bool) -> str:
    """Shell snippet: env-prefixed invocation of the shim with *argv*."""
    import shlex

    parts: list[str] = []
    if with_path:
        parts.append(f"PATH={_PATH_PREFIX}:$PATH")
    parts += [f"{k}={shlex.quote(v)}" for k, v in env.items()]
    parts += ["bash", shlex.quote(asn_path), *(shlex.quote(a) for a in argv)]
    return " ".join(parts)


def shim_exec(
    session: AgentSession,
    *argv: str,
    env_extra: dict[str, str] | None = None,
    timeout: float = 30,
) -> str:
    """Run the deployed ``asn`` shim on *session*'s agent host with its env.

    Drives the real shim -> outbox/tunnel -> daemon path deterministically (no
    dependency on the LLM choosing to call it), reaching the agent host through
    the transport's own exec mechanism (local shell / ``docker exec`` / ``ssh`` /
    ``ssh`` + ``docker exec``). Raises on a non-zero shim exit.
    """
    import shlex

    from agent_session import _transport as T

    t = session.transport
    key = session.session_key
    env = {
        "AGENT_SESSION_KEY": key,
        "ASN_READ_ADDR": T.read_endpoint_addr(t, key),
        "ASN_READ_TOKEN": session.read_token,
    }
    if env_extra:
        env.update(env_extra)

    asn_path = _shim_path(session)
    inner = _shim_inner(asn_path, env, argv, with_path=(t.location == "remote"))

    if t.is_local_native:
        cmd = ["bash", "-lc", inner]
    elif t.location == "local" and t.runtime == "container":
        cmd = ["docker", "exec", t.container, "bash", "-lc", inner]
    elif t.location == "remote" and t.runtime == "native":
        cmd = ["ssh", t.host, f"$SHELL -lc {shlex.quote(inner)}"]
    else:  # remote container
        dexec = f"docker exec {shlex.quote(t.container)} bash -lc {shlex.quote(inner)}"
        cmd = ["ssh", t.host, f"$SHELL -lc {shlex.quote(dexec)}"]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(
            f"shim_exec {argv} failed ({r.returncode}): {r.stderr.strip()}\n{r.stdout.strip()}"
        )
    return r.stdout.strip()


def read_token(session: AgentSession) -> str:
    """Read the session's read-endpoint bearer token from its ``meta.json``."""
    from agent_session import _naming

    meta = json.loads((_naming.SESSIONS_DIR / session.session_key / "meta.json").read_text())
    return meta["read_token"]


def raw_read_request(port: int, payload: dict) -> dict:
    """Send one raw line-JSON request to the daemon read endpoint on the control host.

    Used to exercise the method whitelist directly (the shim only ever speaks
    the whitelisted verbs). The endpoint binds ``0.0.0.0`` so it is reachable on
    loopback from the test process regardless of the session's transport.
    """
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall((json.dumps(payload) + "\n").encode())
        sock.settimeout(10)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode().splitlines()[0])
