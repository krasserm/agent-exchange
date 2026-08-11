# agent-exchange setup

Machine-consumable setup instructions for [agent-exchange](https://github.com/krasserm/agent-exchange), written for a coding agent (Claude Code or Codex) performing the setup on the local machine on a user's behalf. Execute the required steps in order and verify each before continuing. Ask the user before installing system packages and hand interactive logins to the user; they cannot be completed non-interactively.

Steps 1 to 4 are required. Remote sessions (step 5) and container sessions (step 6) are optional; skip them unless the user wants those placements.

## 1. Check prerequisites (required)

The local machine needs:

- macOS or Linux
- `tmux` and `jq`
- [`uv`](https://docs.astral.sh/uv/) and Python 3.13 or newer
- an authenticated `claude` or `codex` executable on `PATH`

```bash
command -v tmux jq uv
command -v claude || command -v codex
```

Install anything missing with the user's consent, using the platform's package manager. Agent authentication (`claude login`, `codex login`) is interactive; ask the user to do it if the agent is not authenticated yet.

## 2. Install the `asn` CLI (required)

```bash
git clone https://github.com/krasserm/agent-exchange.git
cd agent-exchange
uv tool install --editable ./agent-session
```

Keep the checkout: the editable install keeps the sibling `agent-docker` build context available to `asn prep --docker`.

The full `asn` CLI must be discoverable through `PATH` on the local machine before sessions are started, because sessions running directly on that machine invoke it themselves. If `command -v asn` prints nothing:

```bash
uv tool update-shell
```

Then open a new terminal (or reload the shell) and verify:

```bash
command -v asn
asn --help
asn prep --check
```

`asn prep --check` reports local readiness without making changes. Running `asn prep` without `--check` deploys the bundled lifecycle hooks and the in-session `asn` command; they are also deployed automatically at session start, so a passing check is sufficient here.

## 3. Install the `agent-exchange` plugin in the agents (required)

The bundled `agent-exchange` plugin teaches Claude Code or Codex how to operate `asn` and, when explicitly asked, coordinate a team of sessions. From the repository root, install it in each agent that will operate `asn`.

Claude Code:

```bash
claude plugin marketplace add .
claude plugin install agent-exchange@agent-exchange
```

Codex:

```bash
codex plugin marketplace add .
codex plugin add agent-exchange@agent-exchange
```

Start a new agent session after installation so the skills are loaded. The plugin contains one skill for operating sessions and a separate, explicitly activated skill for coordinating a team.

## 4. Smoke test (required)

Start a session in an existing project directory, exchange one message, and stop it:

```bash
asn start /absolute/path/to/project
asn send SESSION_KEY "Reply with exactly the word: pong"
asn messages SESSION_KEY --last 1
asn capture SESSION_KEY --lines 100
asn stop SESSION_KEY
```

`asn start` prints the session key; Claude Code is the default agent and `--agent codex` selects Codex. `asn send` is fire-and-forget, so poll `asn messages` until the reply appears. The daemon starts automatically with the first session and exits when the last managed session stops.

## 5. Remote sessions (optional)

Additional requirements: SSH access from the local machine to the remote machine that succeeds without prompting; [Mutagen](https://mutagen.io/) on the local machine; `tmux`, `jq`, and the authenticated agent executable on the remote machine.

```bash
asn prep --host HOST --agent claude --check
asn prep --host HOST --agent claude
```

The second command deploys the hooks and the in-session `asn` command to the remote machine; the full CLI is not installed there. Repeat with `--agent codex` if Codex will run remotely. Then start with an absolute path on the remote machine:

```bash
asn start /absolute/remote/path --host HOST
```

Pitfalls that need a person:

- On macOS remotes, Claude Code prefers OAuth tokens in the login keychain, which SSH-spawned processes cannot read; they fall back to `~/.claude/.credentials.json`, which goes stale when claude is only used from the GUI. Have the user run `claude login` from an SSH session on that host. A working GUI claude proves nothing about remote readiness.
- Codex credentials are file-only and carry no readable expiry, so prep reports them present without validating; a real turn is the only proof.

## 6. Container sessions (optional)

Additional requirements: Docker and credentials for the selected agent on the machine running the container.

Claude Code: authenticate the container credential store (interactive, hand to the user), then prepare. Preparation builds the `agent-docker` image when it is missing or outdated:

```bash
./agent-docker/agent-docker.sh login
asn prep --docker --agent claude
```

Codex: seed the separate container configuration after a native login:

```bash
codex login
mkdir -p ~/.codex-docker
cp ~/.codex/auth.json ~/.codex-docker/auth.json
asn prep --docker --agent codex
```

For containers on a remote machine, add `--host HOST` to the prep commands; credentials live on that machine. Then:

```bash
asn start /absolute/path/to/project --docker
```

The project directory is mounted inside the container at `/home/coder/workspace`.

Pitfalls that need a person:

- Claude container credentials expire independently of prep's report; a turn can fail with `Login expired - Please run /login` even when prep reports weeks of refresh-token validity. Refresh with `agent-docker.sh login` on the affected machine.
- A fresh `~/.claude-docker/.claude.json` stalls container startup on claude's first-run theme dialog. Have the user run claude interactively in the container once and choose a theme.

## 7. Report

Tell the user what was set up: the `asn` install location (`command -v asn`), the `asn prep --check` results for each prepared placement, the smoke test outcome, which agents received the `agent-exchange` plugin, and which optional steps were skipped.
