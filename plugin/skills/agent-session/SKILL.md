---
name: agent-session
description: Manage coding-agent sessions (Claude Code, codex, ...) programmatically via the asn CLI and tmux. Use when starting, monitoring, or sending messages to agent sessions, or when delegating tasks to parallel agent instances.
---

# agent-session (asn CLI)

Manage interactive coding-agent sessions inside tmux via a background daemon. Supports multiple agents (currently `claude` and `codex`); the agent is chosen at `start` time and every other command is agent-agnostic, keyed by session.

## Quick Reference

```bash
asn start ~/Development/myproject               # Start session (default agent: claude), prints KEY; no prompt text
asn start ~/Development/myproject --agent codex # Start with a specific agent
asn start ~/Development/myproject --resume abc123      # Resume conversation
asn start ~/Development/myproject --worktree feature   # Start in git worktree (claude only)
asn start /srv/proj --host example-host          # Run on a remote host over SSH
asn start ~/Development/myproject --docker       # Run in agent-docker (add --host for remote)
asn reattach KEY                           # Rebuild the local proxy for a disconnected remote/container session
asn list                                   # List active sessions (incl. agent)
asn status KEY                             # Detailed status
asn send KEY "do something"                # Send a message (fire-and-forget)
asn messages KEY                           # Recent assistant messages
asn capture KEY                            # Current terminal output
asn stop KEY                               # Stop session
asn stop --all                             # Stop all sessions
```

Keys are hex strings; unique prefixes suffice (e.g. `a3f`).

Use `asn <command> --help` for full options on any subcommand. The agent is selected only on `start`; `list`/`status`/`send`/`messages`/`capture`/`stop` work the same regardless of agent.

## Typical Workflow

`asn start` takes no prompt text and no `--prompt` flag; send a task (if any) afterwards with `asn send`.

`asn send` returns after the terminal submission or confirmation attempt; it does not wait for the agent's turn to finish. Poll `asn status`, then inspect the response and resulting artifacts:

```bash
asn start ~/Development/myproject --agent codex  # Returns KEY
asn send KEY "refactor the auth module"          # Fire-and-forget
# Poll until status is "idle" (no foreground turn is currently observed) or "waiting" (needs input).
# "backgrounded" is NOT done: the turn ended but background work is still running
asn status KEY --json                            # {"status": "working", ...}
asn messages KEY --json                          # Read assistant response when idle
```

Most commands support `--json` for structured output (`asn list --json`, `asn status --json`, `asn messages --json`).

JSON schema differs by command:

- `asn list --json` returns: `session_key`, `agent`, `tmux_session_name`, `start_dir`, `host`, `status`, `session_id`, `background_tasks`, `session_crons`. `host` is the ssh host a remote session runs on, `null` for local sessions.
- `asn status KEY --json` returns the same fields plus `project_dir` and `transcript_path`.

## Key Concepts

- Each session runs a coding agent in a dedicated tmux session.
- The daemon starts with `asn start` (or `asn daemon start`) and exits when its last managed session stops.
- Select the agent with `--agent {claude,codex}` (default: `claude`). New agents are pluggable.
- By default, native Claude uses its configured approval mode and native Codex uses on-request approval with workspace-write sandboxing; `--skip-permissions` selects bypass mode. Managed Claude containers use automatic approval, and managed Codex containers bypass approvals and Codex's own sandbox.
- Some options are claude-only (`--worktree`, `--chrome`, `--remote-control`); using them with another agent is rejected.
- Status values: **idle** (no foreground turn is currently observed; verify output and artifacts before treating work as complete), **working** (processing), **waiting** (permission prompt or elicitation), **backgrounded** (the turn ended but background tasks/crons are still pending; it is not done and returns to `idle` once they finish; the pending counts are in `background_tasks`/`session_crons`), **disconnected** (the local proxy is absent; the inner runtime may still exist, so try `asn reattach KEY`), **ended** (the agent exited).
- Only Claude Code >= 2.1.145 reports `backgrounded`; codex and older agents omit background-task info, so their sessions show `idle` even with background work in flight.
- Remote/container transports: `--host H` runs the agent on a remote machine over SSH; `--docker` (or `--container NAME`) runs it in an `agent-docker` container; combine them for a remote container. The agent runs in a remote/container tmux that can survive link drops (the local proxy auto-reconnects); `asn stop` tears down the inner runtime. From the control host, `send`/`status`/`messages`/`capture`/`stop` work the same way.
- `session_id` may be `null` immediately after starting some agents (e.g. codex assigns one on the first turn) while the session is otherwise ready/idle.
- Inside a managed session, `$AGENT_SESSION_KEY` holds the session's own full 32-char key. It is exported for every transport (local, remote, container) and agent.
- Attach to a session's terminal directly: `tmux attach -t <tmux_session_name>` (shown in `asn list`).
- Keys printed by `asn start` are 32-char hex; `asn list` shows the 8-char prefix. A short prefix is fine for ad-hoc CLI use, but if you're tracking sessions programmatically, store the full key (`asn list --json[].session_key`). Short prefixes can become ambiguous when more sessions are started.
