"""E2E test fixtures for real agent sessions (claude, codex)."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import libtmux
import pytest

from agent_session import AgentSession
from agent_session.agents import Agent, LaunchSpec, get_agent

from agent_session.agents import Transport

from .helpers import (
    CROSS_HOST_A,
    CROSS_HOST_B,
    DEFAULT_TARGETS,
    E2E_BASE,
    parse_target,
    target_skip_reason,
)

DEFAULT_AGENTS = "claude,codex"

# The isolated state root and socket that tests/conftest.py exported before
# anything imported agent_session. Read back from the environment rather than
# re-derived, so pytest_configure below compares agent_session's import-time
# constants against the very values that were exported. A KeyError here means
# tests/conftest.py never ran, which must fail loudly rather than fall back to
# the user's live tree.
E2E_HOME = Path(os.environ["ASN_HOME"])
E2E_SOCKET = Path(os.environ["ASN_SOCKET_PATH"])


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--agents",
        action="store",
        default=DEFAULT_AGENTS,
        help="Comma-separated agents to run e2e tests against (default: claude,codex)",
    )
    parser.addoption(
        "--targets",
        action="store",
        default=DEFAULT_TARGETS,
        help=(
            "Comma-separated transport targets for transport e2e tests. Each is "
            "local-native | local-container | remote-native@HOST | "
            "remote-container@HOST (default: local-native). Unreachable targets "
            "are skipped, not failed."
        ),
    )


def _selected_agents(config: pytest.Config) -> list[str]:
    raw = config.getoption("--agents") or DEFAULT_AGENTS
    return [a.strip() for a in raw.split(",") if a.strip()]


def _selected_targets(config: pytest.Config) -> list[str]:
    raw = config.getoption("--targets") or DEFAULT_TARGETS
    return [t.strip() for t in raw.split(",") if t.strip()]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize e2e tests over ``agent_name`` and/or ``target`` fixtures."""
    if "agent_name" in metafunc.fixturenames:
        agents = _selected_agents(metafunc.config)
        metafunc.parametrize("agent_name", agents, ids=agents)
    if "target" in metafunc.fixturenames:
        targets = _selected_targets(metafunc.config)
        metafunc.parametrize("target", targets, ids=targets)


def pytest_configure(config: pytest.Config) -> None:
    """Refuse to run unless the suite is isolated from the user's live daemon.

    The e2e tests stop sessions, run ``asn stop --all`` and ``asn daemon stop``.
    Against the real daemon that kills every session the user has running, so
    this asserts that ``tests/conftest.py``'s overrides were picked up by the
    (import-time) module constants before any test starts. A failure here means
    something imported ``agent_session`` before that conftest ran.
    """
    from agent_session import _naming
    from agent_session._daemon import LOCK_PATH, PID_PATH, SOCKET_PATH

    mismatches = [
        f"{name}={value}"
        for name, value, expected in (
            ("DAEMON_DIR", _naming.DAEMON_DIR, E2E_HOME),
            ("SOCKET_PATH", SOCKET_PATH, E2E_SOCKET),
            ("PID_PATH", PID_PATH, E2E_HOME / "daemon.pid"),
            ("LOCK_PATH", LOCK_PATH, E2E_HOME / "daemon.lock"),
        )
        if value != expected
    ]
    if mismatches:
        raise pytest.UsageError(
            "e2e isolation not in effect -- refusing to run against the user's "
            f"daemon (expected state root {E2E_HOME}, got: {', '.join(mismatches)})"
        )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark all e2e tests and skip unless explicitly targeted."""
    run_e2e = config.getoption("-m", default="") == "e2e" or any(
        "e2e" in str(a) for a in config.args
    )
    for item in items:
        if "e2e" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)
            if not run_e2e:
                item.add_marker(pytest.mark.skip(reason="e2e tests require -m e2e or explicit path"))


def _mirrored_hosts(config: pytest.Config) -> list[str]:
    """Every remote host this run may have created a mutagen mirror for.

    Both sources count: the remote ``--targets``, and the pair the cross-host
    test uses, which comes from the environment rather than from ``--targets``.
    """
    hosts = {
        t.host for t in (parse_target(s) for s in _selected_targets(config)) if t.host
    }
    if os.environ.get("ASN_E2E_CROSS_HOST") == "1":
        hosts.add(os.environ.get("ASN_E2E_HOST_A", CROSS_HOST_A))
        hosts.add(os.environ.get("ASN_E2E_HOST_B", CROSS_HOST_B))
    return sorted(hosts)


@pytest.fixture(scope="session", autouse=True)
def e2e_host_mirrors(request: pytest.FixtureRequest) -> Iterator[None]:
    """Terminate the mutagen mirrors this run created, when it finishes.

    ``ensure_host_mirror`` stands one sync up per remote host and nothing in
    production ever tears it down, so each run used to leave its syncs behind for
    good. Terminating them here is only safe because the sync name carries this
    state root's discriminator: these are the suite's syncs, never the user's live
    ones for the same hosts. Best-effort by design -- a failure to clean up must
    not fail the run, and ``teardown_host_mirror`` already swallows its own errors.
    """
    yield
    from agent_session import _mirror

    for host in _mirrored_hosts(request.config):
        _mirror.teardown_host_mirror(host)


@pytest.fixture(scope="session", autouse=True)
def e2e_state_root() -> Path:
    """Clear session dirs left under the isolated state root by previous runs.

    Dirs that still carry a ``meta.json`` are kept: the daemon's orphan recovery
    needs them to reap the tmux session (and any remote resources) an interrupted
    run left behind. It unlinks the meta itself once done, so those dirs are swept
    on the run after that.
    """
    sessions = E2E_HOME / "sessions"
    if sessions.exists():
        for child in sessions.iterdir():
            if child.is_dir() and not (child / "meta.json").exists():
                shutil.rmtree(child, ignore_errors=True)
    E2E_HOME.mkdir(parents=True, exist_ok=True)
    return E2E_HOME


@pytest.fixture(scope="session", autouse=True)
def e2e_base_dir() -> Path:
    """Create the base directory, cleaning up leftovers from previous runs."""
    if E2E_BASE.exists():
        for child in E2E_BASE.iterdir():
            if child.name.startswith("e2e-"):
                shutil.rmtree(child, ignore_errors=True)
    E2E_BASE.mkdir(parents=True, exist_ok=True)
    return E2E_BASE


@pytest.fixture
def e2e_project_dir(e2e_base_dir: Path) -> Path:
    name = f"e2e-{uuid.uuid4().hex[:8]}"
    d = e2e_base_dir / name
    d.mkdir()
    os.system(f"git init -q {d}")
    (d / ".gitignore").write_text(".claude/\n.codex/\n")
    os.system(f"git -C {d} add -A && git -C {d} commit -q -m init")
    yield d
    # Small delay to let any in-flight hooks finish before cleanup.
    import time
    time.sleep(0.5)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def tmux_server() -> libtmux.Server:
    return libtmux.Server()


@pytest.fixture
def agent(agent_name: str) -> Agent:
    return get_agent(agent_name)


@pytest.fixture
def require_capability(agent: Agent) -> Callable[..., None]:
    """Skip the test unless the active agent supports the given capabilities."""
    def _require(*capabilities: str) -> None:
        for cap in capabilities:
            if not getattr(agent, f"supports_{cap}", False):
                pytest.skip(f"agent '{agent.name}' does not support {cap}")
    return _require


@pytest.fixture
def claude_only(agent: Agent) -> None:
    """Skip the test for non-claude agents (claude-specific behavior)."""
    if agent.name != "claude":
        pytest.skip(f"test is claude-specific; skipping for '{agent.name}'")


@pytest.fixture
def make_session(
    agent_name: str, monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., AgentSession]:
    """Factory for building (unstarted) AgentSession instances for the agent."""
    monkeypatch.setenv("AGENT_SESSION_DEV_ROOT", str(E2E_BASE.parent))

    def _make(start_dir: Path, **spec_kwargs: object) -> AgentSession:
        # claude runs in auto-accept mode (settings.json defaultMode="auto"),
        # which avoids its "Bypass Permissions mode" prompt. codex has no
        # equivalent auto mode, so it keeps permission bypass for non-interactive
        # runs.
        skip = agent_name == "codex"
        return AgentSession(
            get_agent(agent_name),
            LaunchSpec(start_dir=start_dir, skip_permissions=skip, **spec_kwargs),
        )

    return _make


@pytest.fixture
async def agent_session(
    e2e_project_dir: Path,
    make_session: Callable[..., AgentSession],
) -> AgentSession:
    session = make_session(e2e_project_dir)
    await session.start(timeout=45)
    yield session
    await session.stop()


# --- transport-parametrized fixtures (transport e2e matrix) ----------------


@pytest.fixture
def transport(target: str) -> Transport:
    """The Transport for the current ``--targets`` token."""
    return parse_target(target)


@pytest.fixture
def target_preflight(transport: Transport) -> None:
    """Skip (not fail) if the target's machine/docker/mutagen is unavailable."""
    reason = target_skip_reason(transport)
    if reason:
        pytest.skip(reason)


@pytest.fixture
def make_target_session(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., AgentSession]:
    """Factory for (unstarted) claude sessions on the current transport.

    For container/remote targets the project path is the agent-host path. For
    local targets it is the e2e project dir.
    """
    monkeypatch.setenv("AGENT_SESSION_DEV_ROOT", str(E2E_BASE.parent))

    def _make(start_dir: Path, transport: Transport, **spec_kwargs: object) -> AgentSession:
        # Auto-accept mode via settings.json (defaultMode="auto"); no
        # --dangerously-skip-permissions, so no bypass-permissions prompt.
        return AgentSession(
            get_agent("claude"),
            LaunchSpec(start_dir=start_dir, transport=transport, **spec_kwargs),
        )

    return _make


@pytest.fixture
def project_factory(
    transport: Transport, e2e_base_dir: Path,
) -> Callable[[], Path]:
    """Create git-initialized project dirs on the transport's *agent host*.

    For local transports this is a dir under the local e2e base; for remote
    transports it is created on the remote host over SSH (so remote-native can
    ``cd`` into it and remote-container can bind-mount it). All created dirs are
    removed on teardown.
    """
    import shlex
    import shutil
    import subprocess
    import uuid

    created: list[tuple[bool, str]] = []  # (is_remote, path)

    def _make() -> Path:
        name = f"e2e-{uuid.uuid4().hex[:8]}"
        if transport.location == "remote":
            home = subprocess.run(
                ["ssh", transport.host, 'printf %s "$HOME"'],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            path = f"{home}/agent-session-e2e/{name}"
            subprocess.run(
                ["ssh", transport.host,
                 f"mkdir -p {shlex.quote(path)} && cd {shlex.quote(path)} && "
                 "git init -q && git commit -q --allow-empty -m init"],
                check=True, capture_output=True,
            )
            created.append((True, path))
            return Path(path)
        d = e2e_base_dir / name
        d.mkdir()
        os.system(f"git init -q {d} && git -C {d} commit -q --allow-empty -m init")
        created.append((False, str(d)))
        return d

    yield _make

    for is_remote, path in created:
        if is_remote:
            subprocess.run(
                ["ssh", transport.host, f"rm -rf {shlex.quote(path)}"],
                capture_output=True,
            )
        else:
            shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def target_session(
    transport: Transport,
    target_preflight: None,
    project_factory: Callable[[], Path],
    make_target_session: Callable[..., AgentSession],
) -> AgentSession:
    """A started claude session on the current transport, torn down after."""
    session = make_target_session(project_factory(), transport)
    await session.start(timeout=90)
    yield session
    await session.stop()
