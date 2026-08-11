"""CLI entry point for managing coding-agent sessions.

Usage: asn <command> [options]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from agent_session._client import (
    DaemonClient,
    DaemonError,
    DaemonNotRunningError,
    _is_daemon_running,
)
from agent_session._daemon import PID_PATH, SOCKET_PATH
from agent_session.agents import DEFAULT_AGENT, Transport, agent_names


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    try:
        asyncio.run(args.func(args))
    except DaemonError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


def _build_parser() -> argparse.ArgumentParser:
    agents = ", ".join(agent_names())
    parser = argparse.ArgumentParser(
        prog="asn",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Manage interactive coding-agent sessions inside tmux.",
        epilog=f"""\
concepts:
  Each session runs a coding agent ({agents}) in a dedicated tmux
  session. Sessions are identified by a hex key. Commands that take a KEY
  accept unique prefixes (e.g. "a3f" is enough if unambiguous).

  A background daemon holds live sessions in memory. It auto-starts on the
  first command and auto-exits when the last session is stopped. You never
  need to manage the daemon explicitly.

  Status values: idle (waiting for input), working (processing), waiting
  (permission prompt or elicitation), ended (the agent exited).

  To interact with a session's terminal directly, attach to its tmux
  session: tmux attach -t <tmux_session_name>

workflow:
  asn start ~/myproject         Start a session, prints KEY and tmux name
  asn list                      Show all active sessions
  asn status KEY                Show detailed info for a session
  asn send KEY "do something"   Send a message to the agent
  asn capture KEY               Show current terminal output
  asn messages KEY              Show recent assistant messages
  asn stop KEY                  Stop a session (kills tmux)
  asn stop --all                Stop all sessions

examples:
  asn start ~/myproject                      # default agent ({DEFAULT_AGENT})
  asn start ~/myproject --agent codex        # choose the agent
  asn start ~/myproject --worktree feat      # claude only
  asn start ~/myproject --resume abc123      # resume a conversation
  asn start ~/myproject --docker             # run in an agent-docker container
  asn start /srv/proj --host 192.168.94.50   # run on a remote host (add --docker for a remote container)
  asn reattach a3f                           # reconnect a disconnected remote/container session
  asn status a3f                             # prefix match
  asn send a3f "create a hello.txt file"
  asn messages a3f --last 3
  asn capture a3f --lines 100
  asn list --json
  asn daemon status
""",
    )
    sub = parser.add_subparsers(title="commands")

    # -- daemon --
    daemon_parser = sub.add_parser(
        "daemon",
        help="Manage the background daemon (usually not needed)",
        description="The daemon auto-starts and auto-stops. These commands are for manual control.",
    )
    daemon_sub = daemon_parser.add_subparsers(title="daemon commands")

    p = daemon_sub.add_parser("start", help="Ensure the daemon is running")
    p.set_defaults(func=_cmd_daemon_start)

    p = daemon_sub.add_parser(
        "stop",
        help="Stop the daemon and all sessions",
        description=(
            "Stop the daemon. All active agent sessions are killed "
            "immediately and their tmux sessions removed. Prints how many "
            "sessions were stopped."
        ),
    )
    p.set_defaults(func=_cmd_daemon_stop)

    p = daemon_sub.add_parser("status", help="Check if the daemon is running")
    p.set_defaults(func=_cmd_daemon_status)

    # -- start --
    p = sub.add_parser(
        "start",
        help="Start a new agent session in a tmux window",
        description=(
            "Creates a tmux session, launches the selected agent in it, and "
            "waits for the agent to become ready. Prints the session key and "
            "tmux session name. The session key is used to reference this "
            "session in all other commands. By default, permission prompts are "
            "NOT skipped; use --skip-permissions to opt in."
        ),
    )
    p.add_argument("dir", help="Project directory to run the agent in")
    p.add_argument("--agent", default=DEFAULT_AGENT, choices=agent_names(),
                    help=f"Which agent to launch (default: {DEFAULT_AGENT})")
    p.add_argument("--worktree", metavar="NAME",
                    help="Create/use a git worktree with this name (claude only)")
    p.add_argument("--resume", metavar="ID", dest="resume_session_id",
                    help="Resume an existing conversation by ID")
    p.add_argument("--remote-control", nargs="?", const=True, default=False,
                    metavar="NAME",
                    help="Enable remote control (claude only; optionally with a name)")
    p.add_argument("--chrome", action="store_true",
                    help="Enable browser tool access via --chrome (claude only)")
    p.add_argument("--skip-permissions", action="store_true",
                    help="Skip the agent's permission/approval prompts (unsafe)")
    p.add_argument("--host", metavar="SSH_HOST",
                    help="Run the agent on a remote host reached over SSH "
                         "(e.g. 192.168.94.50). The local tmux proxies it and "
                         "auto-reconnects on network loss.")
    p.add_argument("--docker", action="store_true",
                    help="Run the agent inside an agent-docker container on its "
                         "host (local, or remote with --host).")
    p.add_argument("--container", metavar="NAME",
                    help="Run in a container with this explicit name (implies "
                         "--docker; default name is derived from the session key)")
    p.add_argument("--timeout", type=float, default=20.0, metavar="SECS",
                    help="Max seconds to wait for the agent to start (default: 20)")
    p.add_argument("--json", action="store_true", dest="as_json",
                    help="Output the started session as JSON")
    p.set_defaults(func=_cmd_start)

    # -- prep --
    p = sub.add_parser(
        "prep",
        help="Check and prepare a machine to run remote/container sessions",
        description=(
            "Probe a machine for the prerequisites of a remote (--host) or "
            "containerized (--docker) agent session and prepare what can be "
            "prepared safely: deploy the hook plugin and BUILD the agent-docker "
            "image natively on the host when it is missing or outdated. Missing "
            "system binaries, agent auth, remote sshd GatewayPorts, and Mutagen "
            "are diagnosed with an exact fix, never mutated. With no --host the "
            "local machine is checked. Exits non-zero when the target is not "
            "ready."
        ),
    )
    p.add_argument("--host", metavar="SSH_HOST",
                    help="Prep a remote host over SSH (omit to prep the local machine)")
    p.add_argument("--agent", default=DEFAULT_AGENT, choices=agent_names(),
                    help=f"Which agent's prerequisites to check (default: {DEFAULT_AGENT})")
    p.add_argument("--docker", action="store_true",
                    help="Prep the container transport (docker daemon + agent-docker image build)")
    p.add_argument("--container", metavar="NAME",
                    help="Same as --docker (the container name is irrelevant to prep)")
    p.add_argument("--check", "-n", action="store_true",
                    help="Report only; make no changes; exit non-zero if not ready")
    p.add_argument("--rebuild", action="store_true",
                    help="Force an image rebuild (fresh cache-bust) even if up-to-date")
    p.add_argument("--json", action="store_true", dest="as_json",
                    help="Machine-readable report")
    p.set_defaults(func=_cmd_prep)

    # -- stop --
    p = sub.add_parser(
        "stop",
        help="Stop a session (kills its tmux session)",
        description="Stop a session by key (or prefix), or stop all sessions with --all.",
    )
    p.add_argument("key", nargs="?", metavar="KEY",
                    help="Session key or unique prefix")
    p.add_argument("--all", action="store_true", dest="stop_all",
                    help="Stop all active sessions")
    p.set_defaults(func=_cmd_stop)

    # -- reattach --
    p = sub.add_parser(
        "reattach",
        help="Rebuild the local proxy for a disconnected remote/container session",
        description=(
            "Recreates the local tmux proxy for a session whose proxy was "
            "killed directly (or lost to a network partition that outlived the "
            "reconnect loop). The agent keeps running in its remote/container "
            "tmux; this reconnects to it. Only valid for non-local sessions."
        ),
    )
    p.add_argument("key", metavar="KEY", help="Session key or unique prefix")
    p.set_defaults(func=_cmd_reattach)

    # -- list --
    p = sub.add_parser(
        "list",
        help="List all active sessions with their status",
        description=(
            "Shows session key, agent, status, project directory, and tmux "
            "session name. With --json, returns: session_key, agent, "
            "tmux_session_name, start_dir, host (ssh host for remote "
            "sessions, null for local ones), status, session_id, "
            "background_tasks, session_crons. Use 'asn status KEY --json' for "
            "the richer schema (transcript_path, project_dir)."
        ),
    )
    p.add_argument("--json", action="store_true", dest="as_json",
                    help="Output as JSON array")
    p.set_defaults(func=_cmd_list)

    # -- status --
    p = sub.add_parser(
        "status",
        help="Show detailed status of a session",
        description=(
            "Displays session key, agent, status, session ID, project "
            "directory, tmux session name, and transcript path. With --json, "
            "returns the same fields as 'asn list --json' plus project_dir and "
            "transcript_path."
        ),
    )
    p.add_argument("key", metavar="KEY", help="Session key or unique prefix")
    p.add_argument("--json", action="store_true", dest="as_json",
                    help="Output as JSON object")
    p.set_defaults(func=_cmd_status)

    # -- send --
    p = sub.add_parser(
        "send",
        help="Send a message to the agent (fire-and-forget)",
        description=(
            "Sends a message string to the agent's terminal. The message "
            "is delivered via tmux as if typed by a user. Returns immediately "
            "without waiting for the agent to process the message. Use "
            "'asn status' or 'asn capture' to check progress."
        ),
    )
    p.add_argument("key", metavar="KEY", help="Session key or unique prefix")
    p.add_argument("message", help="Message text to send")
    p.set_defaults(func=_cmd_send)

    # -- messages --
    p = sub.add_parser(
        "messages",
        help="Show recent assistant messages from the agent",
        description=(
            "Retrieves recent assistant messages from the session's event "
            "stream. Messages are captured from the agent's turn-end events. "
            "Use --last N to retrieve more than the most recent message. "
            "On a session whose agent has ended, prints 'Session "
            "ended; no messages available' (or [] with --json)."
        ),
    )
    p.add_argument("key", metavar="KEY", help="Session key or unique prefix")
    p.add_argument("--last", type=int, default=1, metavar="N",
                    help="Number of recent messages to show (default: 1)")
    p.add_argument("--json", action="store_true", dest="as_json",
                    help="Output as JSON array")
    p.set_defaults(func=_cmd_messages)

    # -- capture --
    p = sub.add_parser(
        "capture",
        help="Capture current tmux pane content",
        description=(
            "Returns the last N lines of the tmux pane where the agent is "
            "running. Useful for observing the agent's current output."
        ),
    )
    p.add_argument("key", metavar="KEY", help="Session key or unique prefix")
    p.add_argument("--lines", type=int, default=50, metavar="N",
                    help="Number of lines to capture (default: 50)")
    p.set_defaults(func=_cmd_capture)

    # -- scrollback --
    p = sub.add_parser(
        "scrollback",
        help="Capture the full tmux pane history (all scrollback)",
        description=(
            "Returns the agent pane's entire scrollback, including content that "
            "has scrolled off the visible screen. For remote/container sessions "
            "this reads the agent's own tmux directly, so it works even though "
            "the local proxy pane (a nested 'tmux attach') cannot be scrolled "
            "back interactively."
        ),
    )
    p.add_argument("key", metavar="KEY", help="Session key or unique prefix")
    p.set_defaults(func=_cmd_scrollback)

    return parser


# -- Command handlers --


async def _cmd_daemon_start(_args: argparse.Namespace) -> None:
    client = DaemonClient()
    # Explicit start command: a no-op call with auto_start to bring the daemon up.
    await client.send("list", auto_start=True)
    print("Daemon is running")


async def _cmd_daemon_stop(_args: argparse.Namespace) -> None:
    if not _is_daemon_running():
        print("Daemon is not running")
        return
    client = DaemonClient()
    sessions = await client.send("list")
    n = len(sessions or [])
    await client.send("daemon_stop")
    if n:
        print(f"Daemon stopped ({n} active session(s) killed)")
    else:
        print("Daemon stopped")


async def _cmd_daemon_status(_args: argparse.Namespace) -> None:
    if _is_daemon_running():
        pid = PID_PATH.read_text().strip()
        print(f"Daemon is running (pid={pid})")
    else:
        print("Daemon is not running")


def _print_start_result(result: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2))
        return
    print(f"Started {result['agent']} session {result['session_key']}")
    print(f"Tmux: {result['tmux_session_name']}")


async def _cmd_start(args: argparse.Namespace) -> None:
    abs_dir = os.path.abspath(args.dir)

    remote_control = args.remote_control
    remote_name = None
    if isinstance(remote_control, str):
        remote_name = remote_control
        remote_control = True

    # A remote path must not be resolved against the local filesystem.
    start_dir = args.dir if args.host else abs_dir

    params = {
        "agent": args.agent,
        "start_dir": start_dir,
        "skip_permissions": args.skip_permissions,
        "timeout": args.timeout,
    }
    if args.worktree:
        params["worktree"] = args.worktree
    if args.resume_session_id:
        params["resume_session_id"] = args.resume_session_id
    if remote_control:
        params["remote_control"] = True
        if remote_name:
            params["remote_name"] = remote_name
    if args.chrome:
        params["chrome"] = True
    if args.host:
        params["location"] = "remote"
        params["host"] = args.host
    if args.docker or args.container:
        params["runtime"] = "container"
        if args.container:
            params["container"] = args.container

    client = DaemonClient()
    result = await client.send(
        "start", params, timeout=args.timeout + 10, auto_start=True,
    )

    _print_start_result(result, as_json=args.as_json)


async def _cmd_prep(args: argparse.Namespace) -> None:
    # Prep runs client-side (not through the daemon): it holds no session state
    # and a remote image build can take minutes, which must not stall the daemon.
    from agent_session import _prep

    transport = Transport(
        location="remote" if args.host else "local",
        host=args.host,
        runtime="container" if (args.docker or args.container) else "native",
        container=args.container,
    )
    report = await asyncio.to_thread(
        _prep.prep_target,
        transport,
        agent_name=args.agent,
        check_only=args.check,
        rebuild=args.rebuild,
    )
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(_prep.render_report(report))
    if not report.ready:
        sys.exit(1)


async def _cmd_stop(args: argparse.Namespace) -> None:
    if not args.stop_all and not args.key:
        print("Error: provide a session key or --all", file=sys.stderr)
        sys.exit(1)

    client = DaemonClient()
    if args.stop_all:
        await client.send("stop_all")
    else:
        await client.send("stop", {"key": args.key})


async def _cmd_reattach(args: argparse.Namespace) -> None:
    client = DaemonClient()
    result = await client.send("reattach", {"key": args.key})
    print(f"Reattached {result['agent']} session {result['session_key']}")
    print(f"Tmux: {result['tmux_session_name']}")


def _format_dir(start_dir: str, host: str | None, width: int = 34) -> str:
    """The DIR cell for one session: where it runs, on which machine.

    A remote ``start_dir`` names a path on *another* machine, so it is prefixed
    with its host and left otherwise intact. Only a local path gets the ``~``
    shortening: applying it to a remote path silently reattributes that path to
    the control host, and where the two homes share a prefix -- both
    ``/Users/martin``, or both ``/home/x`` -- the row becomes indistinguishable
    from a local session in the same directory.

    Over-long values are truncated from the left, keeping the leaf directory,
    which is the part that identifies the session at a glance.
    """
    if host:
        cell = f"{host}:{start_dir}"
    else:
        home = os.path.expanduser("~")
        cell = "~" + start_dir[len(home):] if start_dir.startswith(home) else start_dir
    if len(cell) > width:
        cell = "..." + cell[-(width - 3):]
    return cell


def _status_label(session: dict) -> str:
    """Status text with the pending background-work count, e.g. "backgrounded (2)"."""
    label = session.get("status") or "ended"
    pending = (session.get("background_tasks") or 0) + (session.get("session_crons") or 0)
    return f"{label} ({pending})" if pending else label


async def _cmd_list(args: argparse.Namespace) -> None:
    client = DaemonClient()
    try:
        sessions = await client.send("list")
    except (DaemonNotRunningError, ConnectionRefusedError, FileNotFoundError, RuntimeError):
        sessions = []

    if not sessions:
        if args.as_json:
            print("[]")
        else:
            print("No active sessions")
        return

    if args.as_json:
        print(json.dumps(sessions, indent=2))
        return

    # Table output
    labels = [_status_label(s) for s in sessions]
    status_w = max(10, *(len(label) for label in labels))
    print(f"{'KEY':<10} {'AGENT':<8} {'STATUS':<{status_w}} {'DIR':<36} {'TMUX'}")
    for s, label in zip(sessions, labels):
        short_key = s["session_key"][:8]
        agent = s.get("agent", "")
        start_dir = _format_dir(s.get("start_dir", ""), s.get("host"))
        print(f"{short_key:<10} {agent:<8} {label:<{status_w}} {start_dir:<36} {s.get('tmux_session_name', '')}")


async def _cmd_status(args: argparse.Namespace) -> None:
    client = DaemonClient()
    result = await client.send("status", {"key": args.key})

    if args.as_json:
        print(json.dumps(result, indent=2))
        return

    print(f"Session:     {result['session_key']}")
    print(f"Agent:       {result.get('agent', 'N/A')}")
    print(f"Status:      {_status_label(result)}")
    if result.get("session_id"):
        print(f"Session ID:  {result['session_id']}")
    # Which machine the directory is on. Without it "Directory: /srv/proj" reads
    # as a local path, and for a remote session it is not one.
    print(f"Host:        {result.get('host') or 'local'}")
    print(f"Directory:   {result['start_dir']}")
    if result.get("project_dir") != result.get("start_dir"):
        print(f"Project dir: {result['project_dir']}")
    print(f"Tmux:        {result.get('tmux_session_name', 'N/A')}")
    if result.get("transcript_path"):
        print(f"Transcript:  {result['transcript_path']}")


async def _cmd_send(args: argparse.Namespace) -> None:
    client = DaemonClient()
    await client.send("send", {"key": args.key, "message": args.message})


async def _cmd_messages(args: argparse.Namespace) -> None:
    client = DaemonClient()
    result = await client.send("messages", {"key": args.key, "last": args.last})
    messages = result["messages"]

    if not messages:
        if args.as_json:
            print("[]")
            return
        status_result = await client.send("status", {"key": args.key})
        match status_result.get("status"):
            case None | "ended":
                print("Session ended; no messages available")
            case _:
                print("No messages yet")
        return

    if args.as_json:
        print(json.dumps(messages, indent=2))
        return

    for m in messages:
        print(f"[{m['timestamp']}]")
        print(m["message"])
        print()


async def _cmd_capture(args: argparse.Namespace) -> None:
    client = DaemonClient()
    result = await client.send(
        "capture", {"key": args.key, "lines": args.lines},
    )
    print(result["content"])


async def _cmd_scrollback(args: argparse.Namespace) -> None:
    client = DaemonClient()
    result = await client.send("scrollback", {"key": args.key})
    print(result["content"])


if __name__ == "__main__":
    main()
