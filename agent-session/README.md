# agent-session

`agent-session` is the core of `agent-exchange`. It provides the `asn` command and a background daemon for managing terminal-based agent sessions. It also provides the persistent, distributed messaging infrastructure through which those sessions exchange messages.

Starting a session launches a supported agent, currently Claude Code or Codex, in `tmux` and registers it with the daemon under a session key. Humans, scripts, coordinators, and peers address the session by that key through `asn`. The daemon submits each message to the same terminal UI that a human can attach to directly.

Agent hooks write lifecycle events and assistant messages to per-session files. `agent-session` provides remote and container sessions with an `asn` command limited to messaging and inspection, so the full `asn` CLI does not need to be installed separately in those environments. This command supports `send`, `messages`, `status`, and `capture`, but not lifecycle operations such as `start` or `stop`.

When a remote session calls `asn send`, the command appends the message to the session's outbox. A one-way Mutagen sync replicates the session files, including the outbox, to the local machine. The daemon reads the replicated outbox and forwards the message to the target session.

The underlying agent still owns its terminal UI. `agent-session` adds a common control and messaging layer around it.

## Requirements

- macOS or Linux
- `tmux` and `jq`
- [`uv`](https://docs.astral.sh/uv/) and Python 3.13 or newer
- an authenticated `claude` or `codex` executable on `PATH`

Claude Code is the default agent. Codex can be selected with `--agent codex`; agent-specific flags are rejected when the selected agent does not support them.

| Feature | Additional requirements |
| --- | --- |
| Remote sessions | SSH access from the local machine to the remote machine that succeeds without prompting; [Mutagen](https://mutagen.io/) on the local machine; `tmux`, `jq`, and the authenticated agent executable on the remote machine |
| Container sessions | Docker, the sibling [`agent-docker`](../agent-docker/) project, and credentials for the selected agent on the machine running the container |

`asn prep --check` reports missing requirements without making changes; `--host` selects a remote machine and `--docker` includes the container runtime. Run the command without `--check` to deploy the bundled lifecycle hooks and in-session `asn` command. When `--docker` is present, it also builds a missing or outdated `agent-docker` image. System packages, authentication, and SSH settings remain manual tasks.

## Install

From the root of an `agent-exchange` checkout:

```bash
uv tool install --editable ./agent-session
```

The editable source install keeps the sibling `agent-docker` build context available to `asn prep --docker`.

The full `asn` CLI must be discoverable through `PATH` on the local machine before sessions are started, because sessions running directly on that machine invoke it themselves. If `command -v asn` prints nothing, run:

```bash
uv tool update-shell
```

Open a new terminal and verify both lookup and execution:

```bash
command -v asn
asn --help
```

To run `asn` from the checkout during development:

```bash
cd agent-session
uv sync
uv run asn --help
```

This project-local command does not put `asn` on the `PATH` inherited by locally running agent sessions. Install the editable tool as shown above before testing messages sent by one local agent to another.

## First session

Start an agent in an existing project:

```bash
asn start /absolute/path/to/project --agent codex
```

The first argument selects the project directory. It is a directory on the local machine by default, or on the remote machine when `--host` is used. With `--docker`, that host directory is mounted inside the container at `/home/coder/workspace`.

The command prints a session key and a `tmux` session name. Use the key, or an unambiguous prefix, to address the session:

```bash
asn send SESSION_KEY "Inspect the test failures and propose the smallest fix."
asn status SESSION_KEY
asn messages SESSION_KEY --last 3
asn capture SESSION_KEY --lines 100
```

`send` submits text without waiting for the work to finish. `status`, `messages`, and `capture` show the session state, recent responses, and terminal output.

Attach to the `tmux` session name shown by `asn status SESSION_KEY` to use the agent's terminal UI directly:

```bash
tmux attach -t TMUX_SESSION_NAME
```

Detach with `Ctrl-b d`; this does not stop the agent. Stop it explicitly when finished:

```bash
asn stop SESSION_KEY
```

## Common scenarios

### Drive a session from a script

```bash
session_key=$(asn start "$PWD" --agent codex --json | jq -r '.session_key')
asn send "$session_key" "Run the test suite and report actionable failures."
asn status "$session_key" --json
```

Successful submission does not mean the requested work completed successfully.

### Let two agents exchange messages

```bash
frontend_key=$(asn start /path/to/frontend --agent claude --json | jq -r '.session_key')
backend_key=$(asn start /path/to/backend --agent codex --json | jq -r '.session_key')
asn send "$frontend_key" "The backend session is $backend_key. Coordinate with it using asn send."
asn send "$backend_key" "The frontend session is $frontend_key. Reply to it using asn send."
```

Each agent now knows the other's key and can communicate without a coordinator relaying every message.

### Coordinate a team

```bash
coordinator_key=$(asn start /path/to/coordinator-workspace --agent codex --json | jq -r '.session_key')
asn send "$coordinator_key" "Plan this migration. Start specialist sessions with asn, coordinate them, and integrate their work."
```

A coordinator is an ordinary session given that role. It runs directly on the local machine so it can use the full `asn` CLI to start workers or sub-coordinators.

### Run remotely or in a container

For a remote session, first confirm that SSH succeeds without prompting, then prepare the machine and start beside the remote project:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=5 example-host true
asn prep --host example-host --agent codex --check
asn prep --host example-host --agent codex
asn start /srv/project --host example-host --agent codex
```

For a local container:

```bash
asn prep --docker --agent claude
asn start /absolute/path/to/project --docker
```

For a disconnected remote or container session, `asn reattach KEY` attempts to rebuild the local proxy. The inner runtime must still exist, and the daemon must still hold the session in its live registry.

## Development

```bash
uv run pytest
uv run pytest tests/e2e -v --agents claude,codex
```

The second command starts authenticated agents and may consume provider quota.
