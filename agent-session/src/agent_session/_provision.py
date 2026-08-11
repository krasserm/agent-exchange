"""Prepare an agent host to run a session (paths, plugin, container).

The remote/container side of a session carries only the bundled hook (three
plugin files) plus ``jq`` -- no daemon, no ``asn``, no Python. This module
resolves the agent-host paths a launch needs, deploys the plugin to the agent
host's ``~/.agent-session/plugin`` (for containers it arrives via the
``~/.agent-session`` bind mount), starts the per-session agent-docker
container, and writes the inner launch script onto the agent host.

Pure path helpers (``agent_host_*``) are unit-testable without I/O. The
``ensure_*`` / ``write_*`` functions shell out and are wrapped by callers in
``asyncio.to_thread`` so they never block the event loop.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
from pathlib import Path, PurePosixPath

from agent_session import _naming
from agent_session.agents import Agent, Transport

# Fixed identity inside the agent-docker image.
CONTAINER_USER = "coder"
CONTAINER_HOME = f"/home/{CONTAINER_USER}"
CONTAINER_WORKSPACE = f"{CONTAINER_HOME}/workspace"
CONTAINER_AGENT_SESSION = f"{CONTAINER_HOME}/.agent-session"

# The agent-docker image name. ``AGENT_IMAGE`` overrides it (parity with
# agent-docker.sh); both ``asn start`` and ``asn prep`` resolve it here so the
# name has a single source of truth.
DEFAULT_IMAGE = os.environ.get("AGENT_IMAGE", "agent-docker")

# Prepended to PATH in the inner launch script and in remote prereq checks so an
# agent installed under ~/.local/bin (the claude installer default) or Homebrew
# is found even when reached over a login-but-non-interactive SSH shell, which on
# zsh/macOS does not source ~/.zshrc where those dirs are often added.
REMOTE_PATH_PREFIX = "$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin"

# The self-contained plugin files the hook needs on the agent host, plus the
# ``asn`` message-plane shim (deployed executable alongside them).
PLUGIN_FILES = (
    "log-event.sh",
    "asn",
    "claude/.claude-plugin/plugin.json",
    "claude/hooks/hooks.json",
)


class ProvisionError(RuntimeError):
    """A provisioning step (ssh, docker, rsync) failed."""


# --- pure path helpers ------------------------------------------------------


def agent_host_home(transport: Transport, *, local_home: str) -> str:
    """Absolute ``$HOME`` on the host where the agent runtime lives.

    For a container this is the fixed image home; for a native host it is the
    control host's home locally, or the resolved remote home (passed in by the
    caller, which queries it once per host).
    """
    if transport.runtime == "container":
        return CONTAINER_HOME
    return local_home


def agent_host_project(transport: Transport, start_dir: str) -> str:
    """Directory the agent runs in, as seen from its runtime."""
    if transport.runtime == "container":
        # agent-docker bind-mounts the project at a fixed workspace path.
        return CONTAINER_WORKSPACE
    return start_dir


def agent_host_plugin_root(transport: Transport, *, local_home: str) -> str:
    """Plugin root (``--plugin-dir`` parent) as seen from the agent runtime."""
    home = agent_host_home(transport, local_home=local_home)
    if transport.runtime == "container":
        return f"{CONTAINER_AGENT_SESSION}/plugin"
    return f"{home}/.agent-session/plugin"


def agent_host_session_dir(transport: Transport, session_key: str, *, local_home: str) -> str:
    """Per-session dir on the agent host (where the inner launch.sh lives)."""
    home = agent_host_home(transport, local_home=local_home)
    if transport.runtime == "container":
        return f"{CONTAINER_AGENT_SESSION}/sessions/{session_key}"
    return f"{home}/.agent-session/sessions/{session_key}"


def host_agent_session_root(transport: Transport, *, remote_home: str | None) -> str:
    """Filesystem path of the asn state root on the agent *host* machine.

    Everything physically written for a session hangs off this one root: the
    per-session dirs, the deployed plugin, and -- for containers -- the
    ``~/.agent-session`` bind-mount source that surfaces inside the container as
    :data:`CONTAINER_AGENT_SESSION`.

    A local agent host shares the control host's state root, so the root is
    :data:`~agent_session._naming.DAEMON_DIR` rather than ``$HOME`` -- otherwise
    a local container's hook writes its events into ``$HOME/.agent-session``
    while the daemon watches ``ASN_HOME``, and the session never reports
    readiness. A remote agent host has its own tree under its own home (no
    ``ASN_HOME`` there: the remote side runs no daemon), which the per-host
    mirror surfaces locally. ``remote_home`` is required for remote transports.
    """
    if transport.location == "remote":
        if remote_home is None:
            raise ValueError("remote transport requires a resolved remote home")
        return f"{remote_home}/.agent-session"
    return str(_naming.DAEMON_DIR)


def host_agent_session_dir_on_disk(transport: Transport, session_key: str, *, remote_home: str | None) -> str:
    """Filesystem path of the per-session dir on the agent *host* machine.

    This is where files are physically written: for a container it is the
    bind-mount source on the host (which surfaces inside the container under
    :data:`CONTAINER_AGENT_SESSION`); for a native host it is under that host's
    own state root. ``remote_home`` is required for remote transports.
    """
    root = host_agent_session_root(transport, remote_home=remote_home)
    return f"{root}/sessions/{session_key}"


# --- plugin checksum --------------------------------------------------------


def plugin_checksum(plugin_src: Path) -> str:
    """Stable checksum of the three plugin files, for staleness checks."""
    h = hashlib.sha256()
    for rel in PLUGIN_FILES:
        h.update(rel.encode())
        h.update(b"\0")
        h.update((plugin_src / rel).read_bytes())
        h.update(b"\0")
    return h.hexdigest()


# --- exec helpers -----------------------------------------------------------


def _run(cmd: list[str], *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd, input=input_bytes, capture_output=True,
    )
    if proc.returncode != 0:
        raise ProvisionError(
            f"command failed ({proc.returncode}): {' '.join(shlex.quote(c) for c in cmd)}\n"
            f"{proc.stderr.decode(errors='replace')}"
        )
    return proc


def resolve_remote_home(host: str) -> str:
    """Query and return ``$HOME`` on a remote host (login shell for PATH)."""
    proc = _run(["ssh", host, "$SHELL -lc 'printf %s \"$HOME\"'"])
    home = proc.stdout.decode().strip()
    if not home:
        raise ProvisionError(f"could not resolve remote home on {host}")
    return home


def write_agent_host_file(
    transport: Transport, path_on_host: str, content: str, *, executable: bool = False
) -> None:
    """Write *content* to *path_on_host* on the agent host machine.

    For a local agent host this is a direct filesystem write; for a remote one
    it is streamed over SSH. The path is the bind-mount *source* for containers
    (so it surfaces inside the container), never an in-container path.
    """
    parent = str(Path(path_on_host).parent)
    if transport.location == "remote":
        if not transport.host:
            raise ValueError("remote transport requires a host")
        script = f"mkdir -p {shlex.quote(parent)} && cat > {shlex.quote(path_on_host)}"
        if executable:
            script += f" && chmod +x {shlex.quote(path_on_host)}"
        _run(["ssh", transport.host, script], input_bytes=content.encode())
    else:
        p = Path(path_on_host)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        if executable:
            p.chmod(0o755)


def ensure_plugin(transport: Transport, plugin_src: Path, *, remote_home: str | None) -> None:
    """Deploy the three plugin files to the agent host if missing/outdated.

    Idempotent: a ``.checksum`` marker beside the deployed plugin is compared
    against the local source; deployment is skipped when they match.
    """
    checksum = plugin_checksum(plugin_src)
    dest_root = f"{host_agent_session_root(transport, remote_home=remote_home)}/plugin"

    if _plugin_up_to_date(transport, dest_root, checksum):
        return

    for rel in PLUGIN_FILES:
        src = plugin_src / rel
        dest = f"{dest_root}/{rel}"
        write_agent_host_file(
            transport,
            dest,
            src.read_text(),
            executable=rel.endswith(".sh") or rel == "asn",
        )
    write_agent_host_file(transport, f"{dest_root}/.checksum", checksum)


def _plugin_up_to_date(transport: Transport, dest_root: str, checksum: str) -> bool:
    marker = f"{dest_root}/.checksum"
    if transport.location == "remote":
        try:
            proc = subprocess.run(
                ["ssh", transport.host, f"cat {shlex.quote(marker)} 2>/dev/null"],
                capture_output=True,
            )
        except OSError:
            return False
        return proc.returncode == 0 and proc.stdout.decode().strip() == checksum
    p = Path(marker)
    return p.exists() and p.read_text().strip() == checksum


# --- container lifecycle ----------------------------------------------------


def container_run_command(
    transport: Transport,
    *,
    agent: Agent,
    image: str,
    project_on_host: str,
    agent_session_on_host: str,
    config_dir_on_host: str,
    gitconfig_on_host: str | None,
    credentials_readonly: bool = False,
) -> list[str]:
    """``docker run`` argv that starts the per-session container detached.

    Mirrors agent-docker's hardening/mounts and adds the agent's own
    credential mounts (via :meth:`Agent.container_credential_mounts`, e.g.
    claude's ``~/.claude-docker`` -> ``~/.claude`` or codex's ``~/.codex-docker``
    -> ``~/.codex``) so the in-container agent is authenticated, plus the
    ``~/.agent-session`` bind mount so the in-container hook's events land on the
    agent host (then a one-way Mutagen mirror carries them to the control host
    for remote hosts). ``sleep infinity`` keeps it alive independent of any
    client.

    The credential mount is writable by default, so in-container token refresh
    persists back to the host. Pass ``credentials_readonly=True`` to overlay the
    token file read-only where the agent supports it -- which prevents an
    expired-token container from writing empty credentials back over the host's
    real ones, at the cost of in-container refresh not persisting.
    """
    # docker reads a non-absolute -v source as a *named volume*, so a relative
    # (or unexpanded '~') project path silently mounts a fresh empty directory
    # at the workspace and the agent works in it, reporting success.
    # Agent.validate rejects this earlier for remote starts; this is the guard
    # for any other caller, since the failure is otherwise invisible.
    if not PurePosixPath(project_on_host).is_absolute():
        raise ValueError(
            f"container project path must be absolute on the agent host: "
            f"{project_on_host!r}"
        )
    argv = [
        "docker", "run", "-d", "--name", transport.container or "",
        # Reach the daemon's read endpoint via the host gateway (message plane).
        "--add-host", "host.docker.internal:host-gateway",
        "-e", "LANG=C.UTF-8", "-e", "LC_ALL=C.UTF-8",
    ]
    for mount in agent.container_credential_mounts(
        config_dir_on_host=config_dir_on_host,
        container_home=CONTAINER_HOME,
        readonly=credentials_readonly,
    ):
        argv += ["-v", mount]
    argv += [
        "-v", f"{agent_session_on_host}:{CONTAINER_AGENT_SESSION}",
        "-v", f"{project_on_host}:{CONTAINER_WORKSPACE}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--memory", "8g",
        "--pids-limit", "256",
    ]
    if gitconfig_on_host:
        argv += ["-v", f"{gitconfig_on_host}:{CONTAINER_HOME}/.gitconfig:ro"]
    argv += [image, "sleep", "infinity"]
    return argv


def ensure_container(
    transport: Transport,
    *,
    agent: Agent,
    image: str,
    project_on_host: str,
    agent_session_on_host: str,
    config_dir_on_host: str,
    gitconfig_on_host: str | None,
) -> None:
    """Start the per-session container detached (idempotent on its name)."""
    if transport.runtime != "container" or not transport.container:
        raise ValueError("ensure_container requires a named container transport")

    # Ensure the agent's container config dir (and any files its mounts require)
    # exist on a local host, so a bind-mount source is not auto-created by docker
    # as a root-owned empty dir/file. On a remote host the user is expected to
    # have logged the agent's container config dir in already.
    if transport.location == "local":
        agent.prepare_container_config(Path(config_dir_on_host))

    run_argv = container_run_command(
        transport,
        agent=agent,
        image=image,
        project_on_host=project_on_host,
        agent_session_on_host=agent_session_on_host,
        config_dir_on_host=config_dir_on_host,
        gitconfig_on_host=gitconfig_on_host,
    )
    inspect = ["docker", "inspect", "-f", "{{.State.Running}}", transport.container]
    rm = ["docker", "rm", "-f", transport.container]

    if transport.location == "remote":
        host = transport.host
        running = subprocess.run(
            ["ssh", host, f"$SHELL -lc {shlex.quote(shlex.join(inspect))}"],
            capture_output=True,
        )
        if running.returncode == 0 and running.stdout.decode().strip() == "true":
            return
        subprocess.run(["ssh", host, f"$SHELL -lc {shlex.quote(shlex.join(rm))}"], capture_output=True)
        _run(["ssh", host, f"$SHELL -lc {shlex.quote(shlex.join(run_argv))}"])
    else:
        running = subprocess.run(inspect, capture_output=True)
        if running.returncode == 0 and running.stdout.decode().strip() == "true":
            return
        subprocess.run(rm, capture_output=True)
        _run(run_argv)


def build_inner_launch_script(
    agent: Agent,
    spec,
    *,
    session_key: str,
    session_dir: Path,
    transport: Transport,
    local_home: str,
    read_addr: str,
    read_token: str,
) -> str:
    """Inner launch script run *inside* the agent's remote/container tmux.

    Exports ``AGENT_SESSION_KEY`` (the hook's on/off switch), the message-plane
    env (``ASN_READ_ADDR`` / ``ASN_READ_TOKEN`` for the read endpoint; the shim
    derives its own outbox path and ``from`` field from ``AGENT_SESSION_KEY``),
    puts the plugin dir (which holds the ``asn`` shim) on ``PATH``, cd's into the
    agent-host project, and execs the agent with ``--plugin-dir`` pointing at the
    agent-host plugin path.
    """
    plugin_root = Path(agent_host_plugin_root(transport, local_home=local_home))
    project = agent_host_project(transport, str(spec.start_dir))
    cmd = agent.build_launch_command(
        spec, session_key=session_key, session_dir=session_dir, plugin_dir=plugin_root,
    )
    prelude = "".join(
        f"{line}\n"
        for line in agent.launch_shell_prelude(transport, plugin_root=plugin_root)
    )
    return (
        "#!/bin/bash\n"
        "unset VIRTUAL_ENV VIRTUAL_ENV_PROMPT\n"
        f'export PATH={shlex.quote(str(plugin_root))}:"{REMOTE_PATH_PREFIX}:$PATH"\n'
        f"export AGENT_SESSION_KEY={session_key}\n"
        f"export ASN_READ_ADDR={shlex.quote(read_addr)}\n"
        f"export ASN_READ_TOKEN={shlex.quote(read_token)}\n"
        # A failed cd must stop the launch. There is no `set -e` here (the
        # agent-specific prelude below may legitimately return non-zero), so an
        # unguarded cd would fall through and start the agent in whatever
        # directory the tmux opened in -- the wrong project, silently.
        #
        # The `exit 1` is the part that matters. The message on stderr is a
        # breadcrumb for a hand-run of this script and nothing more: the script
        # *is* the agent tmux's command and there is no `remain-on-exit`, so
        # exiting destroys the window and takes the pane's output with it. What
        # the user actually sees is the readiness timeout from
        # `AgentSession._start_proxied`, which is where an actionable diagnosis
        # for this belongs (as `_prep.diagnose_container_start_failure` already
        # does for a missing image) -- not a second channel out of here.
        f"cd {shlex.quote(project)} || {{ "
        f"echo asn: cannot enter project dir {shlex.quote(project)} "
        f"on the agent host >&2; exit 1; }}\n"
        f"{prelude}"
        f"{cmd}\n"
    )
