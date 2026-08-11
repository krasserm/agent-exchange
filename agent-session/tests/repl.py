"""Single-session REPL for interactive testing of AgentSession."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from agent_session import AgentSession, AgentStatus, LaunchSpec, get_agent
from agent_session.agents import agent_names

STATUS_SYMBOLS = {
    AgentStatus.IDLE: "\033[33m[idle]\033[0m",
    AgentStatus.WORKING: "\033[32m[working]\033[0m",
    AgentStatus.WAITING: "\033[35m[waiting]\033[0m",
}


async def status_watcher(session: AgentSession) -> None:
    """Print status changes as they happen."""
    last_status: AgentStatus | None = None
    while True:
        info = await session.get_agent_info()
        if info is None:
            if last_status is not None:
                print("\n\033[31m[session ended]\033[0m")
                last_status = None
        elif info.status != last_status:
            symbol = STATUS_SYMBOLS.get(info.status, f"[{info.status}]")
            print(f"\r{symbol}", end="", flush=True)
            last_status = info.status
        await asyncio.sleep(0.3)


async def run_repl(session: AgentSession) -> None:
    watcher = asyncio.create_task(status_watcher(session))
    loop = asyncio.get_event_loop()

    try:
        while True:
            try:
                line = await loop.run_in_executor(None, lambda: input("\n> "))
            except EOFError:
                break

            line = line.strip()
            if not line:
                continue

            if line == "/quit":
                break
            elif line == "/stop":
                print("Stopping session (tmux killed) ...")
                await session.stop()
                print("Session stopped. Use /quit to exit.")
                continue
            elif line == "/info":
                info = await session.get_agent_info()
                if info is None:
                    print("No active session.")
                else:
                    print(f"  session_id: {info.session_id}")
                    print(f"  status:     {info.status}")
                    print(f"  transcript: {info.transcript_path}")
                continue
            elif line == "/tmux":
                name = await session.get_tmux_session_name()
                if name:
                    print(f"  tmux session: {name}")
                    print(f"  attach:       tmux attach -t {name}")
                else:
                    print("  tmux session gone.")
                continue
            elif line.startswith("/send "):
                content = line[len("/send "):].strip()
                if not content:
                    print("Usage: /send <message>")
                    continue
                await session.send_user_message(content)
                continue
            elif line.startswith("/messages"):
                parts = line.split()
                n = int(parts[1]) if len(parts) > 1 else 1
                messages = await session.get_assistant_messages(last=n)
                if not messages:
                    print("  No messages yet.")
                else:
                    for m in messages:
                        print(f"  [{m.timestamp}] {m.message}")
                continue

            await session.send_user_message(line)
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive REPL for a single AgentSession.",
    )
    parser.add_argument(
        "start_dir",
        type=Path,
        help="Directory where the agent runs.",
    )
    parser.add_argument(
        "--agent",
        default="claude",
        choices=agent_names(),
        help="Which agent to launch (default: claude).",
    )
    parser.add_argument(
        "--resume",
        dest="resume_session_id",
        default=None,
        help="Resume an existing session.",
    )
    parser.add_argument(
        "--worktree",
        default=None,
        help="Use a git worktree (claude only).",
    )
    parser.add_argument(
        "--skip-permissions",
        action="store_true",
        help="Skip the agent's approval prompts (unsafe).",
    )
    parser.add_argument(
        "--remote-control",
        metavar="NAME",
        default=None,
        help="Enable remote control with the given session name (claude only).",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    session = AgentSession(
        get_agent(args.agent),
        LaunchSpec(
            start_dir=args.start_dir.expanduser(),
            resume_session_id=args.resume_session_id,
            worktree=args.worktree,
            skip_permissions=args.skip_permissions,
            remote_control=args.remote_control is not None,
            remote_name=args.remote_control,
        ),
    )

    print(f"Starting {args.agent} in {args.start_dir} ...")
    try:
        await session.start(timeout=45)
    except TimeoutError:
        print(f"Timed out waiting for {args.agent} to start.", file=sys.stderr)
        sys.exit(1)

    info = await session.get_agent_info()
    print(f"Session {info.session_id if info else '(pending)'} started.")
    print(f"Tmux: tmux attach -t {await session.get_tmux_session_name()}")
    print("Commands: /info, /tmux, /send <msg>, /messages [N], /stop, /quit")

    try:
        await run_repl(session)
    finally:
        print("\nStopping session ...")
        await session.stop()
        print("Done.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
