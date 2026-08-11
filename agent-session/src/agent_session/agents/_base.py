from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Transport:
    """Where an agent session actually runs.

    Two orthogonal properties: *location* (on the control host or on a
    machine reached over SSH) and *runtime* (directly on its host or inside
    a docker container on its host). The default, ``local`` + ``native``,
    is exactly the historical behavior (agent runs on the control host).
    """

    location: str = "local"        # "local" | "remote"
    host: str | None = None        # ssh target when location == "remote"
    runtime: str = "native"        # "native" | "container"
    container: str | None = None   # container name when runtime == "container"

    @property
    def is_local_native(self) -> bool:
        return self.location == "local" and self.runtime == "native"

    def validate(self) -> None:
        """Raise ``ValueError`` on an internally inconsistent descriptor."""
        if self.location not in ("local", "remote"):
            raise ValueError(f"invalid transport location: {self.location!r}")
        if self.runtime not in ("native", "container"):
            raise ValueError(f"invalid transport runtime: {self.runtime!r}")
        if self.location == "remote" and not self.host:
            raise ValueError("remote transport requires a host")
        if self.location == "local" and self.host:
            raise ValueError("local transport must not set a host")


@dataclass(frozen=True)
class LaunchSpec:
    """Options describing how to launch an agent session.

    Not all options are supported by every agent; ``Agent.validate``
    rejects unsupported combinations.
    """

    start_dir: Path
    resume_session_id: str | None = None
    worktree: str | None = None
    skip_permissions: bool = False
    remote_control: bool = False
    remote_name: str | None = None
    chrome_access: bool = False
    transport: Transport = Transport()


class Agent(ABC):
    """A pluggable coding-agent CLI (claude, codex, ...).

    Encapsulates everything agent-specific: the launch command, the
    tmux session-name prefix, supported features, and how a project
    directory and transcript path are derived. Everything else
    (tmux management, event monitoring, the daemon) is agent-agnostic.
    """

    #: Stable identifier used on the CLI (``--agent <name>``) and in metadata.
    name: str
    #: Prefix for tmux session names so agents don't collide.
    tmux_prefix: str
    #: Feature capabilities.
    supports_worktree: bool = False
    supports_chrome: bool = False
    supports_remote_control: bool = False
    #: Whether the agent can run on a remote host / in a container (the
    #: transport descriptor). Both default to True since the hook + tmux
    #: machinery is agent-agnostic; an agent can opt out if it cannot.
    supports_remote: bool = True
    supports_container: bool = True

    #: Host directory (a name under ``$HOME``) holding this agent's *container*
    #: credentials, bind-mounted into the container so the in-container agent
    #: shares the host's login. Mirrors agent-docker's ``~/.claude-docker``.
    #: ``None`` means the agent needs no credential mount.
    container_config_dirname: str | None = None

    #: Readiness detection. Agents that emit a lifecycle event at launch set
    #: ``ready_event`` (e.g. claude's ``SessionStart``). Agents that only emit
    #: lifecycle events once the first turn begins (e.g. codex) instead set
    #: ``ready_marker`` to a substring that appears in the TUI once it is ready
    #: to accept input.
    ready_event: str | None = "SessionStart"
    ready_marker: str | None = None

    #: One-time startup confirmation prompts to auto-dismiss while waiting for
    #: readiness. Each entry is ``(marker_substring, keys)`` where *keys* is a
    #: sequence of tmux key names (e.g. ``("Down", "Enter")``) sent in order
    #: when *marker_substring* first appears in the TUI. Handles prompts that a
    #: fresh agent config raises -- e.g. codex's "trust this directory?" (Enter)
    #: or claude's "Bypass Permissions mode" warning, whose default choice is
    #: "No, exit" so it must be navigated to "Yes, I accept" (Down, Enter).
    #: Consulted for both event- and marker-based readiness.
    startup_prompts: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def ready_from_pane(self, pane: str) -> bool:
        """Return whether a captured TUI *pane* shows the agent is ready.

        Consulted only for marker-based readiness (``ready_event is None``).
        The default matches :attr:`ready_marker` as a substring; agents whose
        readiness needs more than one substring -- e.g. a prompt indicator that
        must be present *while no startup modal is* -- override this.
        """
        return self.ready_marker is not None and self.ready_marker in pane

    def validate(self, spec: LaunchSpec) -> None:
        """Raise ``ValueError`` if *spec* requests unsupported features."""
        if spec.worktree is not None and not self.supports_worktree:
            raise ValueError(f"agent '{self.name}' does not support --worktree")
        if spec.chrome_access and not self.supports_chrome:
            raise ValueError(f"agent '{self.name}' does not support --chrome")
        if spec.remote_control and not self.supports_remote_control:
            raise ValueError(f"agent '{self.name}' does not support --remote-control")
        spec.transport.validate()
        if spec.transport.location == "remote" and not spec.start_dir.is_absolute():
            # Nothing on the control host can interpret this: '~' is expanded by
            # the *remote* shell (we never run one on this path) and a relative
            # path anchors to an unknown remote cwd. Both fail silently
            # downstream -- docker makes a non-absolute -v source into an empty
            # named volume, and an unexpanded `cd '~/proj'` leaves the agent in
            # $HOME -- so refuse here, where we can still say why.
            raise ValueError(
                f"remote start_dir must be an absolute path on the agent host: "
                f"{str(spec.start_dir)!r} (a leading '~' is not expanded locally)"
            )
        if spec.transport.location == "remote" and not self.supports_remote:
            raise ValueError(f"agent '{self.name}' does not support remote hosts")
        if spec.transport.runtime == "container" and not self.supports_container:
            raise ValueError(f"agent '{self.name}' does not support containers")

    @abstractmethod
    def build_launch_command(
        self,
        spec: LaunchSpec,
        *,
        session_key: str,
        session_dir: Path,
        plugin_dir: Path | None = None,
    ) -> str:
        """Return the shell command that launches the agent in a tmux pane.

        The returned command does not include the ``AGENT_SESSION_KEY``
        export; the caller prepends that so hook subprocesses inherit it.

        ``plugin_dir`` is the root of the bundled plugin assets *as seen on
        the host where the agent runs* (the control host for local sessions,
        or the remote/container path for non-local transports). When ``None``
        it defaults to this package's local plugin dir (``plugin_dir()``).
        """

    def launch_shell_prelude(
        self, transport: Transport, *, plugin_root: Path
    ) -> list[str]:
        """Extra shell lines for the inner launch script, agent-specific.

        Emitted after the PATH/env exports and the project ``cd``, before the
        agent command, in the tmux that runs a remote/container session. Used to
        work around agent-specific environment quirks. ``plugin_root`` is the
        plugin dir as seen on the agent host (holds the ``asn`` shim). The base
        implementation adds nothing.
        """
        return []

    def container_credential_mounts(
        self,
        *,
        config_dir_on_host: str,
        container_home: str,
        readonly: bool = False,
    ) -> list[str]:
        """Docker ``-v`` values that bind this agent's credentials into a container.

        Each entry is a ``host:container[:ro]`` spec. ``config_dir_on_host`` is
        the agent's :attr:`container_config_dirname` resolved against the agent
        host's home; ``container_home`` is the container image's ``$HOME``.
        ``readonly`` requests an extra read-only overlay of the token file where
        the agent supports one (prevents an expired-token container from writing
        empty credentials back over the host's real ones). The default returns
        no mounts (agent needs none).
        """
        return []

    def prepare_container_config(self, config_dir_on_host: Path) -> None:
        """Create any host files the credential mounts require to exist.

        Called for *local* containers before ``docker run`` so a bind-mount
        source is never auto-created by docker as a root-owned empty dir/file.
        The base implementation just ensures the config dir exists.
        """
        config_dir_on_host.mkdir(parents=True, exist_ok=True)

    def project_dir(
        self, start_dir: Path, worktree: str | None = None, *, local: bool = True
    ) -> Path:
        """Directory the agent actually operates in (worktree-aware).

        *start_dir* arrives normalized for its transport -- resolved against this
        filesystem for a local session, verbatim for a remote one, where the path
        belongs to another machine and resolving it here would rewrite any prefix
        that happens to be a symlink *on the control host* (on macOS ``/home`` is
        one). Implementations must not resolve it again.

        *local* says whether *start_dir* names a path on **this** filesystem
        (local-native). Implementations that inspect the filesystem to place the
        agent -- e.g. finding the repo root a worktree hangs off -- must consult
        it only when this is true; otherwise they would answer a question about
        the agent host from the control host's disk.
        """
        return start_dir

    def transcript_path(self, project_dir: Path, session_id: str) -> Path | None:
        """Best-effort transcript path when the agent does not report one.

        Answers for *this* machine only: implementations may join onto the local
        ``$HOME``, so the caller must not ask about an agent running elsewhere
        (``AgentSession`` asks only for local-native sessions). Returns ``None``
        when the path is unknown; callers should prefer the ``transcript_path``
        carried on events.
        """
        return None

    def plugin_dir(self) -> Path:
        """Root of this package's bundled plugin assets."""
        import agent_session

        return Path(agent_session.__file__).parent / "plugin"
