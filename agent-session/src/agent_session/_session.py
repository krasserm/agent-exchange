from __future__ import annotations

import asyncio
import dataclasses
import logging
import secrets
import shlex
import subprocess
from pathlib import Path

from agent_session import _naming, _provision
from agent_session import _transport as T
from agent_session._models import AgentInfo, AgentStatus, AssistantMessage
from agent_session._monitor import EventMonitor
from agent_session._naming import (
    generate_session_key,
    get_dev_root,
    make_session_dir,
    make_tmux_session_name,
)
from agent_session._tmux import TmuxManager
from agent_session.agents import Agent, LaunchSpec, Transport

logger = logging.getLogger(__name__)

# Budget for confirming the local proxy attached to the agent-host tmux. Each
# probe shells out to the agent host (ssh, or ssh + docker exec), so one attempt
# can itself take seconds under load -- the previous 20s allowed only a handful
# of tries on remote-container and expired while the attach was still coming up.
# It is a ceiling, not a delay: the wait returns as soon as a client appears.
PROXY_ATTACH_TIMEOUT = 60.0

# How long to wait for the agent's UserPromptSubmit ack after pressing Enter,
# per attempt. Generous enough for a remote session, whose events reach the
# daemon through the Mutagen mirror (poll interval + watcher debounce) rather
# than a local file write.
SUBMIT_CONFIRM_TIMEOUT = 5.0


class AgentSession:
    """Manages an interactive coding-agent session inside a tmux session.

    Agent-specific behavior (launch command, project layout, transcript
    path) is delegated to the supplied :class:`Agent`; everything else
    (tmux, event monitoring, lifecycle) is agent-agnostic.

    Where the agent runs is captured by the session's :class:`Transport`. For
    ``local + native`` (the default) the agent runs directly in the local tmux
    session, exactly as it always has. For every other transport the agent runs
    inside a *remote/container* tmux and the local tmux session holds a
    reconnecting proxy that forwards ``send``/``capture`` to it.
    """

    def __init__(self, agent: Agent, spec: LaunchSpec) -> None:
        agent.validate(spec)
        self._agent = agent
        self._spec = spec

        # Resolve the transport: derive a per-session container name when the
        # caller did not pin one, so teardown is unambiguous.
        transport = spec.transport
        self._session_key = generate_session_key()
        if transport.runtime == "container" and transport.container is None:
            transport = dataclasses.replace(
                transport, container=f"{agent.tmux_prefix}-{self._session_key[:8]}"
            )
        self._transport = transport

        # A remote start_dir names a path on the agent host; do not resolve it
        # against the local filesystem.
        if transport.location == "remote":
            self._start_dir = spec.start_dir
        else:
            self._start_dir = spec.start_dir.resolve()
        self._worktree = spec.worktree

        self._dev_root = get_dev_root()
        self._session_name = self._make_local_session_name()
        # The tmux session name on the agent host (None for local-native, where
        # the agent runs directly in the local session).
        self._remote_session_name: str | None = (
            None if transport.is_local_native
            else f"{agent.tmux_prefix}-{self._session_key[:8]}"
        )
        self._session_dir = make_session_dir(self._session_key)
        self._events_path = self._make_events_path()
        # Per-session bearer token for the daemon's read-only TCP endpoint
        # (message plane, docs/architecture.md). Generated once at start and
        # injected into the launch env; the token -- not loopback binding -- is
        # the security boundary, since a container can only reach the endpoint
        # via a non-loopback bind anyway.
        self._read_token = secrets.token_hex(16)
        self._remote_home: str | None = None

        self._tmux = TmuxManager()
        self._monitor: EventMonitor | None = None
        self._send_lock = asyncio.Lock()
        self._started = False

    def _make_local_session_name(self) -> str:
        t = self._transport
        if t.location == "remote":
            # The remote path is not under the local dev root; name the local
            # proxy by host + key instead. tmux session names may not contain
            # '.' or ':', so map the host through a tmux-safe transform
            # (e.g. "192.168.94.50" -> "192_168_94_50").
            host_safe = _naming.tmux_safe(t.host)
            return f"{self._agent.tmux_prefix}-{host_safe}-{self._session_key[:8]}"
        return make_tmux_session_name(
            self._start_dir,
            self._dev_root,
            self._worktree,
            session_key=self._session_key[:8],
            prefix=self._agent.tmux_prefix,
        )

    def _make_events_path(self) -> Path:
        # Local (native or bind-mounted container) sessions write the events
        # file directly under the control-host session dir. Remote sessions
        # write on the agent host; a per-host Mutagen mirror surfaces them under
        # the local mirror tree, which the monitor watches identically.
        if self._transport.location == "remote":
            return _naming.make_mirror_session_dir(
                self._transport.host, self._session_key
            ) / "events.jsonl"
        return self._session_dir / "events.jsonl"

    # --- properties ---------------------------------------------------------

    @property
    def agent(self) -> Agent:
        return self._agent

    @property
    def agent_name(self) -> str:
        return self._agent.name

    @property
    def transport(self) -> Transport:
        return self._transport

    @property
    def remote_session_name(self) -> str | None:
        return self._remote_session_name

    @property
    def start_dir(self) -> Path:
        return self._start_dir

    @property
    def project_dir(self) -> Path:
        # _start_dir is already normalized per transport (see __init__): resolved
        # for a local session, verbatim for a remote one, whose path names a
        # location on the agent host.
        return self._agent.project_dir(
            self._start_dir,
            self._worktree,
            local=self._transport.is_local_native,
        )

    @property
    def session_key(self) -> str:
        return self._session_key

    @property
    def session_name(self) -> str:
        return self._session_name

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    @property
    def read_token(self) -> str:
        """Bearer token validating this session at the daemon read endpoint."""
        return self._read_token

    @property
    def outbox_path(self) -> Path:
        """Local path of this session's append-only message outbox.

        Sits beside the watched ``events.jsonl`` -- the control-host session dir
        for local transports, or the per-host Mutagen mirror for remote ones --
        so the shim's appended ``{to, message}`` lines reach the daemon by the
        same route events do (direct write / bind mount / mirror).
        """
        return self._events_path.parent / "outbox.jsonl"

    async def get_tmux_session_name(self) -> str | None:
        exists = await asyncio.to_thread(self._tmux.session_exists, self._session_name)
        if not exists:
            return None
        return self._session_name

    async def get_agent_info(self) -> AgentInfo | None:
        if not self._started or self._monitor is None:
            return None
        # The agent reported session-end (claude emits SessionEnd; codex does
        # not, so tmux disappearing is handled below).
        if self._monitor.ended:
            return None

        if self._transport.is_local_native:
            # The project dir may have been deleted (e.g., worktree removal on
            # /exit). Treat this as an inactive session.
            if not await asyncio.to_thread(self.project_dir.exists):
                return None
            # The tmux session may have ended (e.g., the user typed /exit). Not
            # all agents emit a session-end event, so treat a vanished tmux
            # session as the session having ended.
            if await self.get_tmux_session_name() is None:
                return None
        else:
            # For a proxied session, a vanished *local* tmux means the proxy is
            # gone (network partition, or the local tmux was killed directly) --
            # not that the agent ended. The remote agent is still alive and the
            # session is recoverable by reattaching.
            if await self.get_tmux_session_name() is None:
                return AgentInfo(
                    session_id=self._monitor.session_id,
                    status=AgentStatus.DISCONNECTED,
                    transcript_path=self._monitor.transcript_path,
                    background_tasks=self._monitor.background_tasks,
                    session_crons=self._monitor.session_crons,
                )

        session_id = self._monitor.session_id
        # Before the first turn, an agent like codex is ready and idle but has
        # not yet assigned a session id or emitted a status.
        status = self._monitor.status or AgentStatus.IDLE
        transcript_path = self._monitor.transcript_path
        if transcript_path is None and session_id is not None:
            transcript_path = self._fallback_transcript_path(session_id)
        return AgentInfo(
            session_id=session_id,
            status=status,
            transcript_path=transcript_path,
            background_tasks=self._monitor.background_tasks,
            session_crons=self._monitor.session_crons,
        )

    def _fallback_transcript_path(self, session_id: str) -> Path | None:
        """Guess the transcript path, for an agent that did not report one.

        Only answerable for local-native. :meth:`Agent.transcript_path` derives a
        path from *this* machine's ``$HOME``, which is where the transcript lives
        only when the agent runs here directly: a container writes it under its
        own home, a remote under the agent host's, and neither is reachable
        through a control-host path. Guessing one anyway yields a path that does
        not exist, and for a remote session it also resolves an agent-host path
        against the local filesystem -- the rewrite that turned a remote
        ``/home/martin/x`` into ``/System/Volumes/Data/home/martin/x`` on macOS.

        ``None`` is the honest answer, and the one :meth:`Agent.transcript_path`
        already documents for "unknown". Sessions whose agent reports the path on
        its events (claude does) never reach here.
        """
        if not self._transport.is_local_native:
            return None
        return self._agent.transcript_path(self.project_dir, session_id)

    # --- launch -------------------------------------------------------------

    async def start(self, timeout: float = 20.0) -> None:
        if self._transport.is_local_native:
            await self._start_local_native(timeout)
        else:
            await self._start_proxied(timeout)
        self._started = True

    async def _start_local_native(self, timeout: float) -> None:
        await asyncio.to_thread(
            self._tmux.create_session, self._session_name, self._start_dir,
        )
        await asyncio.to_thread(
            self._tmux.set_environment,
            self._session_name,
            "AGENT_SESSION_DIR",
            str(self.project_dir),
        )

        # Create the per-session directory for events (and future artifacts).
        await asyncio.to_thread(self._session_dir.mkdir, parents=True, exist_ok=True)

        self._monitor = EventMonitor(self._events_path)
        self._monitor.start()

        cmd = self._agent.build_launch_command(
            self._spec,
            session_key=self._session_key,
            session_dir=self._session_dir,
        )
        # Write the launch command to a script and run it, rather than typing
        # the full command into the shell. Some agents (codex) take a very long
        # command line (per-event hook config), which is unreliable to deliver
        # via tmux send-keys. AGENT_SESSION_KEY is exported inside the script so
        # the agent's hook subprocesses inherit it. ASN_HOME goes with it: the
        # pane may come from a tmux server that was forked by some *other*
        # process, so the state root cannot be assumed to be inherited.
        launch_script = self._session_dir / "launch.sh"
        # unset VIRTUAL_ENV: the tmux server's global env may carry a foreign
        # venv (seeded from whichever process first started the server), which
        # makes `uv` warn on every run in other projects.
        script = (
            "#!/bin/bash\n"
            "unset VIRTUAL_ENV VIRTUAL_ENV_PROMPT\n"
            f"export AGENT_SESSION_KEY={self._session_key}\n"
            f"export ASN_HOME={shlex.quote(str(_naming.DAEMON_DIR))}\n"
            f"{cmd}\n"
        )
        await asyncio.to_thread(launch_script.write_text, script)
        await asyncio.to_thread(
            self._tmux.send_command, self._session_name, f"bash {launch_script}",
        )

        try:
            await self._wait_until_ready(timeout)
        except TimeoutError:
            await self.stop()
            raise

    async def _start_proxied(self, timeout: float) -> None:
        t = self._transport
        local_home = str(Path.home())
        assert self._remote_session_name is not None

        await asyncio.to_thread(self._session_dir.mkdir, parents=True, exist_ok=True)

        # Resolve the remote home once (needed for absolute agent-host paths).
        if t.location == "remote":
            self._remote_home = await asyncio.to_thread(
                _provision.resolve_remote_home, t.host
            )

        # Observe plane: for a remote host, stand up the per-host Mutagen mirror
        # so the agent's events surface in the local mirror tree we watch.
        if t.location == "remote":
            from agent_session import _mirror

            await asyncio.to_thread(_mirror.ensure_host_mirror, t.host)

        # Deploy the hook plugin to the agent host (container sees it via the
        # ~/.agent-session bind mount).
        await asyncio.to_thread(
            _provision.ensure_plugin,
            t,
            self._agent.plugin_dir(),
            remote_home=self._remote_home,
        )

        # The agent host's home: the remote home for a remote transport, the
        # control host's home for a local one. Native paths (plugin dir, project,
        # session dir) are resolved against this; containers use their own fixed
        # home regardless, so this is harmless for them.
        host_home = self._remote_home or local_home

        # Ensure the per-session container is running (detached, bind-mounted).
        if t.runtime == "container":
            as_base = _provision.host_agent_session_root(
                t, remote_home=self._remote_home
            )
            config_dirname = self._agent.container_config_dirname
            assert config_dirname is not None, (
                f"agent '{self._agent.name}' supports containers but defines no "
                "container_config_dirname"
            )
            config_dir = f"{host_home}/{config_dirname}"
            gitconfig = None
            if t.location == "local":
                gc = Path.home() / ".gitconfig"
                gitconfig = str(gc) if gc.exists() else None
            await asyncio.to_thread(
                _provision.ensure_container,
                t,
                agent=self._agent,
                image=_provision.DEFAULT_IMAGE,
                project_on_host=str(self._start_dir),
                agent_session_on_host=as_base,
                config_dir_on_host=config_dir,
                gitconfig_on_host=gitconfig,
            )

        # Write the inner launch script onto the agent host. Inject the
        # message-plane read address (per transport) + bearer token so the
        # bundled `asn` shim can reach the daemon's read endpoint.
        script = _provision.build_inner_launch_script(
            self._agent,
            self._spec,
            session_key=self._session_key,
            session_dir=self._session_dir,
            transport=t,
            local_home=host_home,
            read_addr=T.read_endpoint_addr(t, self._session_key),
            read_token=self._read_token,
        )
        launch_on_disk = (
            _provision.host_agent_session_dir_on_disk(
                t, self._session_key, remote_home=self._remote_home
            )
            + "/launch.sh"
        )
        await asyncio.to_thread(
            _provision.write_agent_host_file, t, launch_on_disk, script, executable=True
        )
        launch_in_runtime = (
            _provision.agent_host_session_dir(t, self._session_key, local_home=host_home)
            + "/launch.sh"
        )

        # Start watching events before launching so none are missed. The
        # watcher needs the events file's parent dir to exist; for a remote
        # session that dir lives under the Mutagen mirror, which has not synced
        # the per-session dir down yet. Pre-create it locally -- the remote dir
        # already exists (launch.sh was just written there), so one-way-replica
        # treats the local dir as consistent and won't revert it.
        await asyncio.to_thread(
            self._events_path.parent.mkdir, parents=True, exist_ok=True
        )
        self._monitor = EventMonitor(self._events_path)
        self._monitor.start()

        # Create the agent's tmux on the agent host, detached, running the inner
        # script -- so it outlives any client / dropped link.
        create = T.create_session_command(t, self._remote_session_name, launch_in_runtime)
        await asyncio.to_thread(self._run_local, create)

        # Create the LOCAL proxy tmux running the reconnect wrapper.
        await self._spawn_local_proxy()

        try:
            await self._wait_until_ready(timeout)
        except TimeoutError:
            # A container that never becomes ready is most often a missing/stale
            # image or a down docker daemon; turn the bare timeout into an
            # actionable "run asn prep ..." diagnosis before tearing down.
            diagnosis: str | None = None
            if t.runtime == "container":
                from agent_session import _prep

                diagnosis = await asyncio.to_thread(
                    _prep.diagnose_container_start_failure, t, _provision.DEFAULT_IMAGE
                )
            await self.stop()
            if diagnosis is not None:
                raise TimeoutError(diagnosis)
            raise

    async def _spawn_local_proxy(self) -> None:
        """Create the local tmux session running ``attach.sh`` (reconnect loop)."""
        assert self._remote_session_name is not None
        attach_script = T.render_reconnect_script(
            self._transport, self._remote_session_name, session_key=self._session_key
        )
        attach_path = self._session_dir / "attach.sh"
        await asyncio.to_thread(attach_path.write_text, attach_script)
        await asyncio.to_thread(
            self._tmux.create_session, self._session_name, Path.home(),
        )
        await asyncio.to_thread(
            self._tmux.send_command, self._session_name, f"bash {attach_path}",
        )

    async def reattach(self) -> None:
        """Rebuild the local proxy from stored state (idempotent attach).

        Used after the local tmux proxy was killed directly. The agent keeps
        running in its remote/container tmux; this recreates the local pane that
        attaches to it. No remote discovery is needed -- the session already
        holds the key, transport, and remote tmux name.
        """
        if self._transport.is_local_native:
            raise RuntimeError("local-native sessions cannot be reattached")
        if await self.get_tmux_session_name() is not None:
            return  # proxy already present
        if self._monitor is None:
            self._monitor = EventMonitor(self._events_path)
            self._monitor.start()
        await self._spawn_local_proxy()
        await self._wait_proxy_attached(timeout=PROXY_ATTACH_TIMEOUT)
        self._started = True

    async def _wait_proxy_attached(self, timeout: float) -> None:
        """Block until the local proxy has attached to the remote/container tmux.

        The reconnect wrapper's ssh/docker-exec/attach takes a moment to
        connect; input sent before it does goes into a pane nobody is forwarding
        yet and is lost. A non-empty ``list-clients`` for the agent-host session
        confirms a client is attached.

        Raises :class:`TimeoutError` when that is never confirmed. It used to
        return anyway, which turned a slow attach into a silently dropped
        message: the caller went on to send, the agent never saw it, and the only
        symptom was a turn that never happened. That is the failure behind
        ``test_disconnected_then_reattach`` on both remote-container cells, where
        the attach outran the old budget under load.
        """
        if self._remote_session_name is None:
            return
        cmd = T.clients_command(self._transport, self._remote_session_name)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        probes = 0
        while loop.time() < deadline:
            proc = await asyncio.to_thread(self._run_local, cmd, False)
            probes += 1
            if proc.returncode == 0 and proc.stdout.strip():
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(
            f"local proxy did not attach to tmux session "
            f"'{self._remote_session_name}' on "
            f"{self._transport.host or 'localhost'} within {timeout}s "
            f"({probes} probes); refusing to send input into a pane that is not "
            "forwarding yet"
        )

    async def _wait_until_ready(self, timeout: float) -> None:
        """Block until the agent is ready to accept input.

        Agents that emit a launch-time lifecycle event wait for it; agents
        that only signal readiness through their TUI poll the pane for a
        marker string. In both cases a concurrent poller dismisses one-time
        startup confirmation prompts (e.g. claude's bypass-permissions warning,
        codex's trust dialog) so a fresh remote/container config does not stall
        startup.
        """
        assert self._monitor is not None
        dismisser = asyncio.create_task(self._dismiss_startup_prompts(timeout))
        try:
            if self._agent.ready_event is not None:
                try:
                    await self._monitor.wait_for_event(
                        self._agent.ready_event, timeout=timeout,
                    )
                except TimeoutError:
                    raise TimeoutError(
                        f"No {self._agent.ready_event} event received within {timeout}s"
                    )
            else:
                await self._wait_for_marker(timeout)
        finally:
            dismisser.cancel()
            try:
                await dismisser
            except asyncio.CancelledError:
                pass

    async def _wait_for_marker(self, timeout: float) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            # Read the LIVE visible frame only. A scrollback-inclusive capture
            # would keep matching a dismissed startup modal's text that lingers
            # in an inline agent's normal-buffer history (codex 0.142.5), so
            # readiness would never fire even with the composer chevron on screen.
            pane = await asyncio.to_thread(
                self._tmux.capture_visible, self._session_name,
            )
            if self._agent.ready_from_pane(pane):
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(
            f"agent '{self._agent.name}' did not signal TUI readiness within {timeout}s"
        )

    async def _dismiss_startup_prompts(self, timeout: float) -> None:
        """Poll the pane and auto-dismiss the agent's one-time startup prompts.

        Each prompt's keys are sent once, the first time its marker appears.
        Runs until cancelled by :meth:`_wait_until_ready` or the deadline.
        """
        prompts = self._agent.startup_prompts
        if not prompts:
            return
        handled: set[str] = set()
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            # Act on the LIVE visible frame: a prompt is dismissed against the
            # modal that is actually on screen now, not one whose text has
            # scrolled into an inline agent's history.
            pane = await asyncio.to_thread(
                self._tmux.capture_visible, self._session_name,
            )
            for marker, keys in prompts:
                if marker in handled or marker not in pane:
                    continue
                for key in keys:
                    await asyncio.to_thread(self._tmux.send_key, self._session_name, key)
                    await asyncio.sleep(0.3)
                handled.add(marker)
            await asyncio.sleep(0.5)

    # --- teardown -----------------------------------------------------------

    async def stop(self) -> None:
        self._started = False
        if self._monitor is not None:
            await self._monitor.stop()
            self._monitor = None

        # Hard-stop the remote agent (kill its tmux session; remove the
        # per-session container). Best-effort: a dead host must not block local
        # teardown.
        t = self._transport
        if not t.is_local_native and self._remote_session_name is not None:
            for cmd in T.teardown_commands(
                t, self._remote_session_name, remove_container=(t.runtime == "container")
            ):
                try:
                    await asyncio.to_thread(self._run_local, cmd, False)
                except Exception:
                    logger.warning("remote teardown command failed: %s", cmd)

        await asyncio.to_thread(self._tmux.kill_session, self._session_name)

    def _run_local(self, command: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run a transport command string on the control host via ``bash -c``."""
        proc = subprocess.run(["bash", "-c", command], capture_output=True)
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"command failed ({proc.returncode}): {command}\n"
                f"{proc.stderr.decode(errors='replace')}"
            )
        return proc

    async def _ensure_active(self) -> None:
        # A session is usable once it has started and its tmux session is
        # alive, even before the first lifecycle event (codex emits no events
        # until the first turn). It stops being usable once the agent reports
        # session-end or the tmux session disappears.
        if not self._started or self._monitor is None:
            raise RuntimeError("No active agent session")
        if self._monitor.ended:
            raise RuntimeError("No active agent session")
        if await self.get_tmux_session_name() is None:
            raise RuntimeError("tmux session no longer exists")

    async def capture(self, lines: int = 50) -> str:
        await self._ensure_active()
        return await self._capture(lines)

    async def scrollback(self) -> str:
        """Return the agent pane's full history (all scrollback).

        Like :meth:`capture` but unbounded, so it surfaces content that has
        scrolled off the visible screen -- the "see older content" that a proxied
        session's nested ``tmux attach`` cannot expose interactively.
        """
        await self._ensure_active()
        return await self._capture(None)

    async def _capture(self, lines: int | None) -> str:
        """Capture the agent pane, reading the tmux where the agent renders.

        For local-native that is the local session. For a proxied session the
        local pane is the reconnecting ``tmux attach`` proxy, pinned to the
        alternate screen with no scrollback -- so we capture the *inner*
        remote/container tmux directly over the transport, which holds the real
        history buffer.
        """
        if self._transport.is_local_native:
            return await asyncio.to_thread(
                self._tmux.capture_pane, self._session_name, lines
            )
        assert self._remote_session_name is not None
        cmd = T.capture_command(
            self._transport, self._remote_session_name, lines=lines
        )
        proc = await asyncio.to_thread(self._run_local, cmd, False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"remote capture failed ({proc.returncode}): "
                f"{proc.stderr.decode(errors='replace')}"
            )
        return proc.stdout.decode(errors="replace")

    async def send_user_message(self, content: str) -> None:
        async with self._send_lock:
            await self._ensure_active()
            assert self._monitor is not None
            await asyncio.to_thread(self._tmux.send_text, self._session_name, content)
            await asyncio.sleep(0.25)
            if content.startswith("/"):
                # A slash command is a TUI command, not a prompt: measured on
                # claude 2.x, `/help` rendered its output and `/exit` ended the
                # session (SessionEnd) with no UserPromptSubmit for either. There
                # is no ack to wait for, so confirming would add the full budget
                # and then fail every working slash command.
                await asyncio.to_thread(self._tmux.send_enter, self._session_name)
                return
            await self._submit_and_confirm()

    async def _submit_and_confirm(self) -> None:
        """Press Enter, wait for the agent's ack, and return either way.

        ``UserPromptSubmit`` fires only when the message starts a new turn. A
        busy session (mid-turn) takes the pasted text into its steering queue
        and injects it into the running turn as a ``queued_command`` attachment,
        emitting no ``UserPromptSubmit`` at all -- so absence of the ack is not
        evidence of non-delivery, and no-ack must not be an error: callers
        reacting to it by resending produced duplicate deliveries.

        The retry Enter still doubles as the flush for the rare genuinely
        parked composer (fresh two-hop attach under load, tmux copy-mode
        wedge); a redundant Enter is safe because an Enter on an empty
        composer submits nothing and emits no event. Both timeouts are logged
        so an unconfirmed send stays visible in the daemon log.
        """
        assert self._monitor is not None
        for attempt in (1, 2):
            # Register the waiter *before* Enter: the ack can arrive within
            # milliseconds on a local transport, and a waiter registered after
            # it would wait for an event that has already gone by. Creating the
            # task and yielding once runs wait_for_event up to its first await,
            # which is where it appends itself to the monitor's waiter list.
            waiter = asyncio.create_task(
                self._monitor.wait_for_event(
                    "UserPromptSubmit", timeout=SUBMIT_CONFIRM_TIMEOUT
                )
            )
            await asyncio.sleep(0)
            await asyncio.to_thread(self._tmux.send_enter, self._session_name)
            try:
                await waiter
                return
            except TimeoutError:
                logger.warning(
                    "no UserPromptSubmit within %.1fs on session %s (attempt %d)",
                    SUBMIT_CONFIRM_TIMEOUT, self._session_name, attempt,
                )
        logger.info(
            "no UserPromptSubmit after two Enters (%.0fs) on session %s; "
            "treating as delivered (busy sessions queue the message without "
            "emitting an ack)",
            2 * SUBMIT_CONFIRM_TIMEOUT, self._session_name,
        )

    async def get_assistant_messages(self, last: int = 1) -> list[AssistantMessage]:
        if self._monitor is None:
            return []
        return self._monitor.get_assistant_messages(last=last)
