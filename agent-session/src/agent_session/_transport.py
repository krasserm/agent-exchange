"""Command builders for reaching an agent's tmux across a transport.

Every supported setup is the product of two orthogonal properties of where the
agent runs (see :class:`agent_session.agents.Transport`):

* **location** -- ``local`` (on the control host) or ``remote`` (over SSH);
* **runtime** -- ``native`` (directly on its host) or ``container`` (in a
  docker container on its host).

The agent always runs inside a *remote/container* tmux so a dropped link only
detaches it. A *local* tmux pane holds the connection and forwards
``send``/``capture``; these builders produce the shell command strings that
pane (and the daemon) run to reach, probe, create, and tear down that tmux.

All functions here are pure string builders -- they take an already-resolved
:class:`Transport` plus absolute paths and return shell command strings, so
they are exhaustively unit-testable without a network or docker. Path
resolution (remote ``$HOME`` etc.) happens in the session/provision layer.

``local + native`` never uses these builders (the daemon drives libtmux
directly, as it always has); they are exercised only for the three non-trivial
transports.
"""

from __future__ import annotations

import os
import shlex

from agent_session.agents import Transport

# UTF-8 locale forwarded into a container's interactive tmux so the agent TUI
# draws Unicode box-drawing instead of the VT100/ASCII fallback (stray _ and q
# glyphs) of the default POSIX locale, which is worst under nested tmux.
_LOCALE_ENV = ["-e", "LANG=C.UTF-8", "-e", "LC_ALL=C.UTF-8"]

# --- message-plane read endpoint addressing ---------------------------------
#
# The daemon's read-only TCP endpoint listens on the control host at CPORT
# (``READ_PORT``). Non-local-native sessions reach it through one of:
#   * a per-session ``ssh -R`` reverse tunnel (remote transports), whose remote
#     bind port is RPORT -- derived per session so two sessions on one host do
#     not collide on the bind; or
#   * the docker host gateway (local container), straight to CPORT.
# See ``docs/architecture.md`` under "Message routing".
READ_PORT = int(os.environ.get("ASN_READ_PORT", "47600"))  # CPORT (control host)
READ_BIND = os.environ.get("ASN_READ_BIND", "0.0.0.0")
RPORT_BASE = int(os.environ.get("ASN_READ_RPORT_BASE", "48000"))
RPORT_SPAN = int(os.environ.get("ASN_READ_RPORT_SPAN", "1000"))


def session_rport(session_key: str) -> int:
    """Deterministic per-session remote bind port for the ``-R`` tunnel.

    Derived from the session key so it is stable across reconnects and unique
    enough that concurrent sessions on one host do not collide on the remote
    loopback bind (``ExitOnForwardFailure`` would otherwise fail the second).
    """
    return RPORT_BASE + (int(session_key[:4], 16) % RPORT_SPAN)


def reverse_tunnel_flags(transport: Transport, session_key: str) -> list[str]:
    """``ssh -R`` flags that forward the agent host back to the read endpoint.

    Native remote: a loopback-only bind (``127.0.0.1:RPORT``) the shim dials
    directly. Container remote: a non-loopback bind (``0.0.0.0:RPORT``, requires
    the remote sshd's ``GatewayPorts``) so the container can reach it through its
    host gateway. ``ExitOnForwardFailure`` makes ssh fail fast if the bind is
    already taken rather than silently dropping the forward.
    """
    rport = session_rport(session_key)
    bind = "127.0.0.1" if transport.runtime == "native" else "0.0.0.0"
    return [
        "-R", f"{bind}:{rport}:127.0.0.1:{READ_PORT}",
        "-o", "ExitOnForwardFailure=yes",
    ]


def read_endpoint_addr(transport: Transport, session_key: str) -> str:
    """``host:port`` the session's shim uses to reach the daemon read endpoint.

    * remote native -> ``127.0.0.1:RPORT`` (the ``-R`` loopback forward);
    * remote container -> ``host.docker.internal:RPORT`` (``-R`` non-loopback
      bind reached via the container's host gateway);
    * local container -> ``host.docker.internal:CPORT`` (host gateway, no tunnel);
    * local native -> ``127.0.0.1:CPORT`` (unused; local-native uses real ``asn``).
    """
    if transport.runtime == "container":
        port = READ_PORT if transport.location == "local" else session_rport(session_key)
        return f"host.docker.internal:{port}"
    if transport.location == "remote":
        return f"127.0.0.1:{session_rport(session_key)}"
    return f"127.0.0.1:{READ_PORT}"


def _runtime_host_cmd(transport: Transport, argv: list[str], *, tty: bool) -> str:
    """Host-level command that runs *argv* where the agent runtime lives.

    For a native runtime that is *argv* itself (tmux is a host process); for a
    container it is wrapped in ``docker exec`` so the command runs inside the
    container's namespace (the only portable way to reach a containerized tmux,
    independent of host kernel -- see ``sandbox/tmux-docker``).
    """
    if transport.runtime == "container":
        if not transport.container:
            raise ValueError("container transport requires a container name")
        pre = ["docker", "exec"]
        if tty:
            pre.append("-it")
            pre += _LOCALE_ENV
        pre.append(transport.container)
        return shlex.join(pre + argv)
    return shlex.join(argv)


def _wrap_host(
    transport: Transport, host_cmd: str, *, tty: bool, tunnel: list[str] | None = None
) -> str:
    """Run *host_cmd* on the agent host: locally as-is, or wrapped in SSH.

    Remote commands go through a login shell (``$SHELL -lc``) so the
    non-interactive SSH ``PATH`` finds a Homebrew tmux/docker (e.g.
    ``/opt/homebrew/bin`` on Apple Silicon), which a bare
    ``ssh host 'tmux ...'`` would miss. ``$SHELL`` is left unquoted inside the
    single-quoted payload so the *remote* shell expands it.

    *tunnel* is an optional list of extra ssh flags (e.g. the message-plane
    ``-R`` reverse tunnel) inserted before the host -- only the long-lived attach
    command carries it, so short-lived probes do not race on the remote bind.
    """
    if transport.location == "remote":
        if not transport.host:
            raise ValueError("remote transport requires a host")
        login = f"$SHELL -lc {shlex.quote(host_cmd)}"
        flag = "-t " if tty else ""
        extra = (" ".join(tunnel) + " ") if tunnel else ""
        return f"ssh {flag}{extra}{shlex.quote(transport.host)} {shlex.quote(login)}"
    return host_cmd


def attach_command(
    transport: Transport, remote_sess: str, *, session_key: str | None = None
) -> str:
    """Command that attaches an interactive client to the agent's tmux.

    When *session_key* is given for a remote transport, the attach ssh also
    carries the per-session ``-R`` reverse tunnel for the message-plane read
    endpoint, so it lives and reconnects with the proxy
    (``docs/architecture.md``, "Message routing").
    """
    inner = _runtime_host_cmd(transport, ["tmux", "attach", "-t", remote_sess], tty=True)
    tunnel = (
        reverse_tunnel_flags(transport, session_key)
        if session_key is not None and transport.location == "remote"
        else None
    )
    return _wrap_host(transport, inner, tty=True, tunnel=tunnel)


def has_session_command(transport: Transport, remote_sess: str) -> str:
    """Command that exits 0 iff the agent's tmux session still exists."""
    inner = _runtime_host_cmd(
        transport, ["tmux", "has-session", "-t", remote_sess], tty=False
    )
    return _wrap_host(transport, inner, tty=False)


def create_session_command(
    transport: Transport, remote_sess: str, launch_path: str
) -> str:
    """Command that creates the agent's tmux *detached*, running *launch_path*.

    ``launch_path`` is an absolute path to the inner launch script on the agent
    host (it exports ``AGENT_SESSION_KEY`` and execs the agent with hooks
    wired). The session is created detached so it outlives any client; the
    local proxy pane attaches to it separately.

    Two scrollback-friendly options are chained onto the new session (the ``;``
    tokens are literal tmux command separators, quoted through the shell):

    * ``terminal-overrides '*:smcup@:rmcup@'`` strips the enter/exit
      alternate-screen capabilities, so when the local proxy attaches with
      ``tmux attach`` the inner client does *not* pin the outer proxy pane to the
      alternate screen. The outer pane then stays on its normal buffer and
      accumulates real scrollback (mouse-wheel / copy-mode work), instead of
      showing only the current screen. An inner app that uses the alternate
      screen itself (a pager, a full-screen TUI) is still absorbed by the inner
      tmux, so this does not pollute the outer scrollback.
    * ``status off`` hides the inner tmux status bar, which would otherwise leak
      into that outer scrollback on redraws and double up with the proxy's own.

    The authoritative full history still lives in this inner tmux and is read
    directly by :func:`capture_command`; these options only improve the *live*
    attached experience.
    """
    inner = _runtime_host_cmd(
        transport,
        [
            "tmux", "new-session", "-d", "-s", remote_sess, f"bash {shlex.quote(launch_path)}",
            ";", "set", "-g", "status", "off",
            ";", "set", "-ga", "terminal-overrides", "*:smcup@:rmcup@",
        ],
        tty=False,
    )
    return _wrap_host(transport, inner, tty=False)


def capture_command(
    transport: Transport, remote_sess: str, *, lines: int | None = None
) -> str:
    """Command whose stdout is the agent tmux pane's content incl. scrollback.

    Captures the *inner* (remote/container) tmux -- where the agent renders
    inline into a real history buffer -- rather than the outer proxy pane. The
    proxy pane is driven by ``tmux attach`` and so exposes only the current
    screen; the inner pane holds the full backlog (pre-attach content included).

    ``lines`` bounds the capture to the last N lines of history; ``None`` returns
    the entire scrollback (``-S -``, from the start of history).
    """
    start = "-" if lines is None else f"-{lines}"
    inner = _runtime_host_cmd(
        transport,
        ["tmux", "capture-pane", "-p", "-t", remote_sess, "-S", start],
        tty=False,
    )
    return _wrap_host(transport, inner, tty=False)


def clients_command(transport: Transport, remote_sess: str) -> str:
    """Command whose stdout is non-empty once a client is attached to the tmux.

    Used after (re)spawning the local proxy to confirm the
    ssh/docker-exec/attach chain has actually connected before sending input --
    otherwise early keystrokes are delivered while the pane is still connecting
    and are lost.
    """
    inner = _runtime_host_cmd(
        transport, ["tmux", "list-clients", "-t", remote_sess, "-F", "x"], tty=False
    )
    return _wrap_host(transport, inner, tty=False)


def teardown_commands(
    transport: Transport, remote_sess: str, *, remove_container: bool
) -> list[str]:
    """Commands that reap the remote agent (hard stop).

    For a container the daemon owns (one per session), removing the container
    kills its in-container tmux and agent in one step. Otherwise kill just the
    agent's tmux session, leaving the host untouched.
    """
    if transport.runtime == "container" and remove_container:
        if not transport.container:
            raise ValueError("container transport requires a container name")
        rm = shlex.join(["docker", "rm", "-f", transport.container])
        return [_wrap_host(transport, rm, tty=False)]
    inner = _runtime_host_cmd(
        transport, ["tmux", "kill-session", "-t", remote_sess], tty=False
    )
    return [_wrap_host(transport, inner, tty=False)]


def render_reconnect_script(
    transport: Transport,
    remote_sess: str,
    *,
    backoff: float = 2.0,
    session_key: str | None = None,
) -> str:
    """Generate the ``attach.sh`` run as the local proxy pane's command.

    The loop reconnects on any link failure and stops *only* when it can
    positively confirm the remote tmux session is gone -- so a network
    partition never falsely tears the session down (proposal section 6). A
    successful ``has-session`` probe reporting no session is the sole exit.

    *session_key* (when set) makes the attach ssh carry the per-session ``-R``
    reverse tunnel for the message-plane read endpoint, so it re-establishes
    every time the loop reconnects.
    """
    attach = attach_command(transport, remote_sess, session_key=session_key)
    probe = has_session_command(transport, remote_sess)
    # ssh returns 255 for its own transport failures; a non-255 exit means the
    # remote command ran and returned its own status.
    return f"""\
#!/bin/bash
# Auto-generated by agent-session. Proxies the local tmux pane to the agent's
# tmux on {transport.location}/{transport.runtime}, reconnecting on link loss.
set -u
backoff={backoff}
while true; do
  {attach}
  rc=$?
  if {probe} >/dev/null 2>&1; then
    # Remote session still exists -> this was a detach or a network blip.
    sleep "$backoff"
    continue
  fi
  # Could not confirm the session is alive. Only a clean (non-SSH-failure)
  # exit means it is genuinely gone; an SSH transport failure (255) means the
  # probe itself could not reach the host, so keep retrying.
  if [ "$rc" = 255 ]; then
    sleep "$backoff"
    continue
  fi
  exit 0
done
"""
