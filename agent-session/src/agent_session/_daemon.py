"""Daemon server for managing AgentSession instances.

Holds live AgentSession objects in memory and serves CLI requests
over a Unix domain socket. Follows the tmux model: auto-starts on
first use, auto-exits when the last session is removed.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import signal
import sys
import time
from collections.abc import MutableMapping
from pathlib import Path

from agent_session import _naming
from agent_session import _transport as T
from agent_session._monitor import watch_jsonl
from agent_session._session import AgentSession
from agent_session.agents import LaunchSpec, Transport, get_agent

logger = logging.getLogger(__name__)

SOCKET_PATH = Path(
    os.environ.get("ASN_SOCKET_PATH", f"/tmp/agent-session-{os.getuid()}.sock")
)
PID_PATH = _naming.DAEMON_DIR / "daemon.pid"
LOCK_PATH = _naming.DAEMON_DIR / "daemon.lock"

# Methods the read-only TCP endpoint will serve (message plane,
# docs/architecture.md, "Message routing"). Deliberately excludes
# start/stop/daemon_stop: the
# endpoint never mutates session lifecycle.
_READ_METHODS = frozenset({"messages", "status", "capture", "scrollback"})

# How long an ended session's directory is kept. Generous on purpose: its
# events.jsonl is the record used to diagnose message-plane bugs, and the tree
# is small (a few KB per session), so age -- not volume -- is the right bound.
RETENTION_DAYS = 7.0


def _reap_remote_from_meta(meta: dict) -> None:
    """Tear down a remote/container session's resources from its ``meta.json``.

    Reconstructs the transport and remote tmux name from durable metadata and
    runs the teardown commands (remote ``tmux kill-session``, ``docker rm -f``).
    Best-effort: an unreachable host is logged and skipped.
    """
    import subprocess

    from agent_session import _transport as T

    td = meta.get("transport") or {}
    if td.get("location", "local") == "local" and td.get("runtime", "native") == "native":
        return  # nothing remote to reap
    remote_sess = meta.get("remote_session_name")
    if not remote_sess:
        return
    transport = Transport(
        location=td.get("location", "local"),
        host=td.get("host"),
        runtime=td.get("runtime", "native"),
        container=td.get("container"),
    )
    for cmd in T.teardown_commands(
        transport, remote_sess, remove_container=(transport.runtime == "container")
    ):
        try:
            # 10s, not 30: an unreachable host is the expensive case here, and
            # a reap that cannot connect in 10s will not connect in 30 either.
            subprocess.run(["bash", "-c", cmd], capture_output=True, timeout=10)
            logger.info("Reaped remote orphan: %s", cmd)
        except Exception:
            logger.warning("Failed to reap remote orphan: %s", cmd)


def select_prunable(
    sessions_dir: Path, *, live_keys: set[str], max_age_days: float = RETENTION_DAYS
) -> list[Path]:
    """Session dirs safe to delete: ended, not live, and older than the cutoff.

    All three conditions are load-bearing. ``meta.json`` absence alone does not
    mean "ended": the dir is created inside ``AgentSession.start()`` and the
    meta is written only after it returns, so a meta-less dir is also every
    session that is currently *starting* -- hence the registry check. And a
    still-present meta means an unreaped session, which recovery owns, not
    retention. The age cutoff is what keeps recent ``events.jsonl`` around:
    it is the evidence used to diagnose message-plane bugs (issue #3).

    Pure selection, no deletion, so the policy is unit-testable without I/O
    beyond a stat.
    """
    if not sessions_dir.exists():
        return []
    cutoff = time.time() - max_age_days * 86400
    prunable: list[Path] = []
    for d in sorted(sessions_dir.iterdir()):
        if not d.is_dir() or d.name in live_keys or (d / "meta.json").exists():
            continue
        try:
            if d.stat().st_mtime < cutoff:
                prunable.append(d)
        except OSError:
            continue
    return prunable


def _build_transport(params: dict) -> Transport:
    """Construct a :class:`Transport` from ``start`` request params.

    Missing keys default to today's ``local + native``. The container name is
    left unresolved here (``None`` for the default per-session container); the
    session derives it from its key.
    """
    return Transport(
        location=params.get("location", "local"),
        host=params.get("host"),
        runtime=params.get("runtime", "native"),
        container=params.get("container"),
    )


class SessionRegistry:
    """Maps session keys to live AgentSession instances."""

    def __init__(self) -> None:
        self._sessions: dict[str, AgentSession] = {}
        # Reverse index: read-endpoint bearer token -> session key. Lets the
        # read endpoint validate a request's token in O(1) without scanning.
        self._tokens: dict[str, str] = {}

    def add(self, session: AgentSession) -> None:
        self._sessions[session.session_key] = session
        self._tokens[session.read_token] = session.session_key

    def remove(self, key: str) -> None:
        self._sessions.pop(key, None)
        self._tokens = {t: k for t, k in self._tokens.items() if k != key}

    def key_for_token(self, token: str | None) -> str | None:
        """Resolve a read-endpoint bearer token to its session key, or None."""
        if not token:
            return None
        return self._tokens.get(token)

    def get(self, key: str) -> AgentSession:
        session = self._sessions.get(key)
        if session is None:
            raise KeyError(f"No session with key '{key}'")
        return session

    def resolve_key(self, prefix: str) -> str:
        """Resolve a key prefix to a full session key.

        Raises KeyError if no match or ambiguous.
        """
        if prefix in self._sessions:
            return prefix
        matches = [k for k in self._sessions if k.startswith(prefix)]
        if len(matches) == 0:
            raise KeyError(f"No session matching '{prefix}'")
        if len(matches) > 1:
            raise KeyError(
                f"Ambiguous prefix '{prefix}' matches: {', '.join(sorted(matches))}"
            )
        return matches[0]

    def all_keys(self) -> list[str]:
        return list(self._sessions)

    @property
    def is_empty(self) -> bool:
        return len(self._sessions) == 0


class DaemonServer:
    """Unix socket server that dispatches requests to AgentSession instances."""

    def __init__(self) -> None:
        self._registry = SessionRegistry()
        self._server: asyncio.Server | None = None
        # Read-only, token-gated TCP endpoint for non-local-native sessions
        # (message plane). Opened in run(), closed in _cleanup().
        self._read_server: asyncio.Server | None = None
        # Per-session outbox watcher tasks (key -> task) that tail each
        # session's outbox.jsonl and dispatch the appended sends.
        self._outbox_tasks: dict[str, asyncio.Task[None]] = {}
        self._should_shutdown = False
        self._lock_fd: int | None = None
        self._bound = False
        # Deferred startup maintenance (orphan reaps), started in run() once the
        # socket is serving and cancelled by _cleanup().
        self._maintenance_task: asyncio.Task[None] | None = None

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            data = await reader.readline()
            if not data:
                return
            request = json.loads(data.decode())
            method = request.get("method", "")
            params = request.get("params", {})

            try:
                result = await self._dispatch(method, params)
                response = {"ok": True, "result": result}
            except Exception as e:
                response = {"ok": False, "error": str(e)}

            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        except Exception:
            logger.exception("Error handling connection")
        finally:
            writer.close()
            await writer.wait_closed()

        if self._should_shutdown:
            self._request_shutdown()

    async def _handle_read_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        """Serve the read-only, token-gated TCP endpoint (message plane).

        Speaks the same line-JSON as the Unix socket but expects an extra
        ``token`` field, validates it against a live session, and only serves
        the read whitelist (``messages`` / ``status`` / ``capture``) -- never the
        lifecycle methods reachable through :meth:`_dispatch`.
        """
        try:
            data = await reader.readline()
            if not data:
                return
            try:
                request = json.loads(data.decode())
                token = request.get("token")
                method = request.get("method", "")
                params = request.get("params", {})
                if self._registry.key_for_token(token) is None:
                    response = {"ok": False, "error": "invalid or missing token"}
                elif method not in _READ_METHODS:
                    response = {"ok": False, "error": f"method not allowed: {method}"}
                else:
                    result = await self._dispatch_read(method, params)
                    response = {"ok": True, "result": result}
            except Exception as e:
                response = {"ok": False, "error": str(e)}

            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        except Exception:
            logger.exception("Error handling read connection")
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dispatch_read(self, method: str, params: dict) -> dict | None:
        match method:
            case "messages":
                return await self._handle_messages(params)
            case "status":
                return await self._handle_status(params)
            case "capture":
                return await self._handle_capture(params)
            case "scrollback":
                return await self._handle_scrollback(params)
            case _:
                raise ValueError(f"method not allowed: {method}")

    def _start_outbox_watcher(self, session: AgentSession) -> None:
        """Tail *session*'s outbox and dispatch each appended message as a send.

        The watcher lives in the daemon because it owns the registry needed to
        deliver to arbitrary target keys. The outbox can trigger only ``send``;
        no other method is reachable through it.
        """
        key = session.session_key
        self._outbox_tasks[key] = asyncio.create_task(
            self._watch_outbox(key, session.outbox_path)
        )

    async def _watch_outbox(self, key: str, path: Path) -> None:
        try:
            async for line in watch_jsonl(path, tail=True):
                to = line.get("to")
                message = line.get("message")
                if not to or message is None:
                    continue
                try:
                    await self._handle_send({"key": to, "message": message})
                except Exception as e:
                    logger.warning("outbox send from %s to %s failed: %s", key, to, e)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("outbox watcher for %s crashed", key)

    async def _cancel_outbox_watcher(self, key: str) -> None:
        task = self._outbox_tasks.pop(key, None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _dispatch(self, method: str, params: dict) -> dict | list | None:
        match method:
            case "start":
                return await self._handle_start(params)
            case "stop":
                return await self._handle_stop(params)
            case "stop_all":
                return await self._handle_stop_all()
            case "reattach":
                return await self._handle_reattach(params)
            case "list":
                return await self._handle_list()
            case "status":
                return await self._handle_status(params)
            case "send":
                return await self._handle_send(params)
            case "messages":
                return await self._handle_messages(params)
            case "capture":
                return await self._handle_capture(params)
            case "scrollback":
                return await self._handle_scrollback(params)
            case "daemon_stop":
                return await self._handle_stop_all()
            case _:
                raise ValueError(f"Unknown method: {method}")

    async def _handle_start(self, params: dict) -> dict:
        agent = get_agent(params.get("agent"))
        transport = _build_transport(params)
        spec = LaunchSpec(
            start_dir=Path(params["start_dir"]),
            resume_session_id=params.get("resume_session_id"),
            worktree=params.get("worktree"),
            skip_permissions=params.get("skip_permissions", False),
            remote_control=params.get("remote_control", False),
            remote_name=params.get("remote_name"),
            chrome_access=params.get("chrome", False),
            transport=transport,
        )
        session = AgentSession(agent, spec)
        timeout = params.get("timeout", 20.0)
        await session.start(timeout=timeout)

        self._registry.add(session)
        self._write_meta(session)
        self._start_outbox_watcher(session)

        tmux_name = await session.get_tmux_session_name()
        return {
            "session_key": session.session_key,
            "tmux_session_name": tmux_name,
            "agent": session.agent_name,
        }

    async def _handle_stop(self, params: dict) -> None:
        key = self._registry.resolve_key(params["key"])
        session = self._registry.get(key)
        await self._cancel_outbox_watcher(key)
        await session.stop()
        self._registry.remove(key)
        self._remove_meta(key)

        if self._registry.is_empty:
            self._should_shutdown = True

    async def _handle_reattach(self, params: dict) -> dict:
        key = self._registry.resolve_key(params["key"])
        session = self._registry.get(key)
        await session.reattach()
        tmux_name = await session.get_tmux_session_name()
        return {
            "session_key": key,
            "tmux_session_name": tmux_name,
            "agent": session.agent_name,
        }

    async def _handle_stop_all(self) -> None:
        for key in list(self._registry.all_keys()):
            session = self._registry.get(key)
            await self._cancel_outbox_watcher(key)
            await session.stop()
            self._registry.remove(key)
            self._remove_meta(key)

        self._should_shutdown = True

    async def _session_to_dict(
        self, key: str, session: AgentSession, *, detailed: bool = False,
    ) -> dict:
        info, tmux_name = await asyncio.gather(
            session.get_agent_info(),
            session.get_tmux_session_name(),
        )
        result = {
            "session_key": key,
            "agent": session.agent_name,
            "tmux_session_name": tmux_name,
            "start_dir": str(session.start_dir),
            # Where the agent actually runs: the ssh host for remote transports,
            # None for local ones (mirrors transport.host in meta.json).
            "host": session.transport.host,
            "status": info.status.value if info else None,
            "session_id": info.session_id if info else None,
            "background_tasks": info.background_tasks if info else 0,
            "session_crons": info.session_crons if info else 0,
        }
        if detailed:
            result["project_dir"] = str(session.project_dir)
            transcript = info.transcript_path if info else None
            result["transcript_path"] = str(transcript) if transcript else None
        return result

    async def _list_entry(self, key: str, session: AgentSession) -> dict:
        """Best-effort list row for a single session.

        ``asn list`` fans this out across every session. If reading one
        session's live state fails transiently (e.g. a libtmux error while the
        tmux server is busy under concurrent load), that one session degrades to
        an unknown-status row instead of aborting the whole request -- otherwise
        a single flaky session makes ``asn list`` return nothing at all,
        nondeterministically, even while every other session is live.
        """
        try:
            return await self._session_to_dict(key, session)
        except Exception:
            logger.warning("list: could not read live state for %s", key, exc_info=True)
            return {
                "session_key": key,
                "agent": session.agent_name,
                "tmux_session_name": None,
                "start_dir": str(session.start_dir),
                "host": session.transport.host,
                "status": None,
                "session_id": None,
                "background_tasks": 0,
                "session_crons": 0,
            }

    async def _handle_list(self) -> list:
        keys = self._registry.all_keys()
        return list(await asyncio.gather(*[
            self._list_entry(k, self._registry.get(k))
            for k in keys
        ]))

    async def _handle_status(self, params: dict) -> dict:
        key = self._registry.resolve_key(params["key"])
        return await self._session_to_dict(key, self._registry.get(key), detailed=True)

    async def _handle_send(self, params: dict) -> None:
        key = self._registry.resolve_key(params["key"])
        session = self._registry.get(key)
        await session.send_user_message(params["message"])

    async def _handle_messages(self, params: dict) -> dict:
        key = self._registry.resolve_key(params["key"])
        session = self._registry.get(key)
        last = params.get("last", 1)
        messages = await session.get_assistant_messages(last=last)
        return {
            "messages": [
                {"message": m.message, "timestamp": m.timestamp.isoformat()}
                for m in messages
            ],
        }

    async def _handle_capture(self, params: dict) -> dict:
        key = self._registry.resolve_key(params["key"])
        session = self._registry.get(key)
        lines = params.get("lines", 50)
        content = await session.capture(lines=lines)
        return {"content": content}

    async def _handle_scrollback(self, params: dict) -> dict:
        key = self._registry.resolve_key(params["key"])
        session = self._registry.get(key)
        content = await session.scrollback()
        return {"content": content}

    def _write_meta(self, session: AgentSession) -> None:
        meta_dir = _naming.SESSIONS_DIR / session.session_key
        meta_dir.mkdir(parents=True, exist_ok=True)
        transport = session.transport
        meta = {
            "session_key": session.session_key,
            "agent": session.agent_name,
            "start_dir": str(session.start_dir),
            "tmux_session_name": session.session_name,
            "transport": {
                "location": transport.location,
                "host": transport.host,
                "runtime": transport.runtime,
                "container": transport.container,
            },
            "remote_session_name": session.remote_session_name,
            "read_token": session.read_token,
        }
        (meta_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    def _remove_meta(self, session_key: str) -> None:
        (_naming.SESSIONS_DIR / session_key / "meta.json").unlink(missing_ok=True)

    def _request_shutdown(self) -> None:
        if self._server:
            self._server.close()

    def _snapshot_orphans(self) -> list[tuple[Path, dict]]:
        """List the orphan metas to reap, as ``(meta_file, meta)`` pairs.

        Reads only; the expensive tmux/ssh/docker work is :meth:`_reap_orphans`.
        Taken **before** the socket binds even though the reaps run after, and
        the snapshot is what makes that split safe: :meth:`_reap_orphans` never
        consults the registry, and :meth:`_handle_start` writes ``meta.json``
        before adding the session to it, so a deferred scan of a live directory
        could reap a session that started microseconds ago (``kill-session`` +
        ``docker rm -f`` against a *running* agent). A snapshot taken before the
        daemon serves anything cannot contain a session this daemon started.
        Scanning is ~1ms for a full state root, so it costs startup nothing.
        """
        if not _naming.SESSIONS_DIR.exists():
            return []

        snapshot: list[tuple[Path, dict]] = []
        for meta_dir in _naming.SESSIONS_DIR.iterdir():
            meta_file = meta_dir / "meta.json"
            if not meta_file.exists():
                continue
            try:
                snapshot.append((meta_file, json.loads(meta_file.read_text())))
            except Exception:
                logger.warning("Failed to read orphan meta: %s", meta_dir)
                meta_file.unlink(missing_ok=True)
        return snapshot

    def _reap_orphans(self, snapshot: list[tuple[Path, dict]]) -> None:
        """Tear down the orphans in *snapshot* (blocking; run in a thread).

        Kills any orphaned *local* tmux session, and -- for non-local-native
        transports recorded in ``meta.json`` -- reaps the *remote* resources
        (remote tmux session, per-session container) so nothing leaks across a
        daemon restart. This is the backstop for the "local tmux killed
        directly" case: the daemon is gone so it cannot reconcile lazily, so on
        the next start it tears the remote down from durable meta.

        Every call here is blocking (libtmux, ``subprocess.run`` over ssh), so
        callers must dispatch it with :func:`asyncio.to_thread` -- awaiting it on
        the event loop would leave the socket accepting connections while no
        request is ever served.
        """
        from agent_session._tmux import TmuxManager
        tmux = TmuxManager()

        for meta_file, meta in snapshot:
            try:
                tmux_name = meta.get("tmux_session_name")
                if tmux_name and tmux.session_exists(tmux_name):
                    logger.info("Killing orphaned tmux session: %s", tmux_name)
                    tmux.kill_session(tmux_name)
                _reap_remote_from_meta(meta)
            except Exception:
                logger.warning("Failed to process orphan: %s", meta_file.parent)
            meta_file.unlink(missing_ok=True)

    async def _recover_orphans(self) -> None:
        """Snapshot and reap orphans in one step (off the startup path)."""
        snapshot = self._snapshot_orphans()
        if not snapshot:
            return
        await asyncio.to_thread(self._reap_orphans, snapshot)

    def _prune_old_sessions(self) -> None:
        """Delete ended session dirs past the retention cutoff (blocking)."""
        import shutil

        for d in select_prunable(
            _naming.SESSIONS_DIR, live_keys=set(self._registry.all_keys())
        ):
            try:
                shutil.rmtree(d)
                logger.info("Pruned ended session dir: %s", d.name)
            except OSError:
                logger.warning("Could not prune session dir: %s", d)

    async def _run_deferred_maintenance(self, snapshot: list[tuple[Path, dict]]) -> None:
        """Background startup maintenance: reap *snapshot*'s orphans, then prune.

        Runs after the socket is serving, so a single unreachable host's ssh
        reap can no longer stall startup past the client's 5s connect budget.
        Cancellation stops the daemon *waiting* on the reaps; a call already in
        flight finishes in its worker thread, bounded by the subprocess timeout.

        Pruning goes last so a dir this run just reaped (its meta unlinked) is
        eligible in the same pass once old enough, and it reads the registry at
        that point rather than from the startup snapshot -- by then the daemon
        has been serving for a while and may already hold live sessions.
        """
        try:
            if snapshot:
                await asyncio.to_thread(self._reap_orphans, snapshot)
            await asyncio.to_thread(self._prune_old_sessions)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Deferred startup maintenance failed", exc_info=True)

    def _acquire_singleton_lock(self) -> bool:
        """Try to become the sole daemon via an exclusive advisory lock.

        Returns True if the lock was acquired, False if another daemon already
        holds it. Without this guard, concurrent cold-starts (e.g. a CLI call
        racing a poller) each unlink-and-rebind the socket, orphaning the prior
        daemon together with its in-memory session registry.
        """
        _naming.DAEMON_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    def _release_singleton_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # closing the fd releases the flock
            self._lock_fd = None

    async def _cleanup(self) -> None:
        """Stop all sessions and remove daemon files we own."""
        if self._read_server is not None:
            self._read_server.close()
            self._read_server = None

        # The daemon auto-exits as soon as the last session stops, which can be
        # well before the reaps finish; stop waiting on them rather than leaving
        # the task pending at loop shutdown.
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass
            self._maintenance_task = None

        for key in list(self._registry.all_keys()):
            try:
                await self._cancel_outbox_watcher(key)
                session = self._registry.get(key)
                await session.stop()
                self._remove_meta(key)
            except Exception:
                logger.warning("Failed to stop session %s during cleanup", key)
            self._registry.remove(key)

        # Only the daemon that actually bound the socket may remove the shared
        # socket/pid files. A daemon that lost the singleton race (or never
        # bound) must not delete the live owner's files out from under it.
        if self._bound:
            SOCKET_PATH.unlink(missing_ok=True)
            try:
                if PID_PATH.read_text().strip() == str(os.getpid()):
                    PID_PATH.unlink(missing_ok=True)
            except FileNotFoundError:
                pass

        self._release_singleton_lock()

    async def run(self) -> None:
        """Start the daemon server. Blocks until shutdown."""
        _naming.DAEMON_DIR.mkdir(parents=True, exist_ok=True)

        if not self._acquire_singleton_lock():
            logger.info(
                "Another daemon already holds the lock; exiting (pid=%d)",
                os.getpid(),
            )
            return

        # Snapshot the orphans now (~1ms), reap them once we are serving. The
        # reaps used to run here, before the bind and the PID write: a single
        # unreachable host blew the client's 5s connect budget, and with no PID
        # file yet every other client concluded no daemon was running, spawned
        # its own, lost the flock and exited silently -- so the whole batch
        # failed, not just the first caller.
        orphan_snapshot = self._snapshot_orphans()

        SOCKET_PATH.unlink(missing_ok=True)
        PID_PATH.write_text(str(os.getpid()))

        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(SOCKET_PATH),
        )
        self._bound = True
        logger.info("Daemon started (pid=%d, socket=%s)", os.getpid(), SOCKET_PATH)

        # Message plane: open the read-only, token-gated TCP endpoint beside the
        # Unix socket. Best-effort -- a bind failure (e.g. the port is taken)
        # must not take the whole daemon down; only the read on-ramp is lost.
        try:
            self._read_server = await asyncio.start_server(
                self._handle_read_connection, host=T.READ_BIND, port=T.READ_PORT,
            )
            logger.info("Read endpoint listening on %s:%d", T.READ_BIND, T.READ_PORT)
        except OSError as e:
            logger.warning(
                "Could not open read endpoint on %s:%d: %s",
                T.READ_BIND, T.READ_PORT, e,
            )
            self._read_server = None

        self._maintenance_task = asyncio.create_task(
            self._run_deferred_maintenance(orphan_snapshot)
        )

        try:
            await self._server.serve_forever()
        finally:
            await self._cleanup()
            logger.info("Daemon stopped")


def scrub_environment(env: MutableMapping[str, str]) -> None:
    """Drop venv traces the daemon inherited from the shell that started it.

    The tmux server forks as the daemon's child on first session creation and
    seeds its global environment from the daemon's, so anything left here leaks
    into every pane. Removes ``VIRTUAL_ENV``/``VIRTUAL_ENV_PROMPT`` and strips
    the venv bin dirs (asn's own and the previously active one) from ``PATH``,
    matching entries exactly and leaving the rest of ``PATH`` untouched.
    """
    old_venv = env.pop("VIRTUAL_ENV", None)
    env.pop("VIRTUAL_ENV_PROMPT", None)

    path = env.get("PATH")
    if path is None:
        return
    venv_bins = {sys.prefix + "/bin"}
    if old_venv:
        venv_bins.add(old_venv + "/bin")
    env["PATH"] = os.pathsep.join(
        entry for entry in path.split(os.pathsep) if entry not in venv_bins
    )


def _setup_logging() -> None:
    log_path = _naming.DAEMON_DIR / "daemon.log"
    _naming.DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _run() -> None:
    scrub_environment(os.environ)
    _setup_logging()
    server = DaemonServer()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, server._request_shutdown)

    await server.run()


if __name__ == "__main__":
    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, SystemExit):
        pass
