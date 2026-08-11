# Transport verification

Repeatable procedure for verifying every transport placement in [architecture.md](architecture.md) with both agents: the full matrix is 2 agents (claude, codex) x 6 cells (local native, local container, and native plus container on each remote host). The commands below use an example setup with a mac mini (192.168.94.51) and a linux box (192.168.94.50) as the two remote hosts; substitute your own SSH hosts. Last full 12-cell run 2026-08-11; all 12 cells passed after completing claude container onboarding locally and refreshing claude container credentials on the linux box.

Per cell, verify: prep check, start, presence in `asn list`, status transition idle to working to idle, a real send with the assistant reply observed through `asn messages`, nonempty `asn capture`, and stop with exact remote tmux and container cleanup.

## 1. Check prerequisites

Run `asn prep --check` for every cell. Exit code 0 means the placement is ready. Read the warnings anyway: an expired-credential warning predicts a runtime auth failure even when prep reports ready. The failure appears at turn time, not start time; the session starts, the TUI renders, and the first prompt fails with `Login expired - Please run /login` in the pane.

```bash
for agent in claude codex; do
  uv run asn prep --agent $agent --check
  uv run asn prep --agent $agent --docker --check
  for host in 192.168.94.51 192.168.94.50; do
    uv run asn prep --agent $agent --host $host --check
    uv run asn prep --agent $agent --host $host --docker --check
  done
done
```

Notes that save time:

- Codex credentials are file-only and carry no readable expiry, so prep reports them "present" without validating; a real turn is the only proof.
- `asn prep --host H --docker --rebuild` force-rebuilds the `agent-docker` image natively on a host. Prep in `--check` mode never builds.
- On the mac mini the claude binary is at `~/.local/bin/claude` and is only on PATH in interactive shells; use the absolute path for direct SSH probes.

## 2. Run the transport e2e suite (claude cells)

The suite covers start-to-idle, prompt round trip, terminal capture, event flow through bind mount and Mutagen mirror, and stop reaping remote tmux and containers. It hardcodes claude in `tests/e2e/conftest.py` (`make_target_session`), so it only covers the six claude cells; `--agents` does not affect it:

```bash
uv run pytest tests/e2e/test_transport_e2e.py -v \
  -k "TestHappyPath or TestTeardownReaping" \
  --targets local-native,local-container,remote-native@192.168.94.51,remote-container@192.168.94.51,remote-native@192.168.94.50,remote-container@192.168.94.50
```

The suite isolates its daemon state, creates its own project directories, and removes them on teardown. `test_stop_reaps_remote_and_container[local-native]` skips by design. Expect roughly 25 seconds per target. A credential-blocked target fails `test_send_and_receive`; record it as failed, do not substitute the other agent.

## 3. Manual cells (codex, or any cell needing direct evidence)

Codex cells and re-tests of individual cells run manually against the same isolation the test suite uses, so the user's live daemon is never touched. Export the values from `tests/conftest.py`:

```bash
export ASN_HOME="$HOME/.agent-session-e2e"
export ASN_SOCKET_PATH="/tmp/agent-session-e2e-$(id -u).sock"
export ASN_READ_PORT=47601
export AGENT_SESSION_DEV_ROOT="$HOME/Development"
```

Create a disposable git-initialized project per cell. Local projects go under `~/Development/.agent-session-e2e/e2e-<name>` (the e2e fixtures sweep leftovers named `e2e-*` on their next run). Remote paths must be absolute on the agent host; a leading `~` is rejected. Then per cell:

```bash
uv run asn start <project> --agent <agent> [--host HOST] [--docker]   # prints KEY and tmux name
uv run asn list                                    # key present, status column
uv run asn status <key>
uv run asn send <key> "Reply with exactly the word: pong"
uv run asn list                                    # poll: idle -> working -> idle
uv run asn messages <key> --last 1                 # poll until it shows pong
uv run asn capture <key>                           # nonempty pane content
uv run asn stop <key>
```

Cleanup is verified by the 8-character key prefix, which names every resource: tmux sessions locally and on the agent host, and the container (`cc-<key8>` or `cx-<key8>`). After stop there must be zero matches:

```bash
tmux ls -F '#S' | grep <key8>                                  # local (outer proxy for remote cells)
ssh HOST 'PATH=$PATH:/opt/homebrew/bin:/usr/local/bin tmux ls -F "#S" 2>/dev/null' | grep <key8>
ssh HOST 'docker ps -a --format "{{.Names}}"' | grep <key8>
```

## Pitfalls that cost time on the first run

- **macOS keychain vs file credentials.** Claude Code on macOS prefers OAuth tokens in the login keychain, which SSH-spawned processes (including asn's remote tmux) cannot read. They fall back to `~/.claude/.credentials.json`, which goes stale when claude is only used from the GUI. A working GUI claude therefore proves nothing about remote-native readiness. Fix: run `claude login` from an SSH session on the host so the file store is refreshed.
- **Container credentials are file-only** (`~/.claude-docker` on the runtime host). Refresh with `agent-docker.sh login` there, or with the image alone (the script is just a wrapper for these mounts):

  ```bash
  docker run -it --rm \
    -v ~/.claude-docker:/home/coder/.claude \
    -v ~/.claude-docker/.claude.json:/home/coder/.claude.json \
    agent-docker claude login
  ```

  The login prints an OAuth URL to open in any browser; paste the resulting code back into the prompt.
- **A fresh container config stalls startup on claude's theme dialog.** `agent-docker.sh` seeds `~/.claude-docker/.claude.json` with `{}` when missing. Without completed onboarding, claude opens its first-run theme chooser, which the startup-prompt dismisser does not handle, and every start on that placement fails with `No SessionStart event received` while credentials are perfectly valid (`claude -p` in the same container answers fine). Fix: run claude interactively in the container once and choose a theme.
- **Prep "Ready" is not "authenticated".** Treat credential warnings as failures-to-be for real turns and fix auth before blaming the transport. This holds even when prep validates a refresh-token expiry: on 2026-08-11 the linux box container reported 14.5 days of validity and still failed its first turn with `Login expired`.

## Cleanup after manual runs

- Stop every started session; confirm `asn list` (isolated env) shows none. The daemon then exits on its own.
- Remove the disposable project dirs locally and on the remote hosts.
- Tear down the Mutagen mirrors the isolated run created. Sync names carry a state-root discriminator, so this never touches the live daemon's `asn-<host>` syncs:

  ```bash
  uv run python -c "from agent_session import _mirror; [_mirror.teardown_host_mirror(h) for h in ('192.168.94.51','192.168.94.50')]"
  ```

  (run with the isolation env exported).
- Session record dirs under `~/.agent-session` on remote hosts are retained by design; leave them.

## Result matrix, 2026-08-11 (12 cells)

| Agent | Placement | Host | Verified via | Result |
| --- | --- | --- | --- | --- |
| claude | native | local | e2e | pass |
| claude | container | local | e2e | pass (after completing container onboarding) |
| claude | native | mac mini | e2e | pass |
| claude | container | mac mini | e2e | pass |
| claude | native | linux box | e2e | pass |
| claude | container | linux box | e2e | pass (after container login) |
| codex | native | local | manual | pass |
| codex | container | local | manual | pass |
| codex | native | mac mini | manual | pass |
| codex | container | mac mini | manual | pass |
| codex | native | linux box | manual | pass |
| codex | container | linux box | manual | pass |

The codex cells ran concurrently under the isolated env; every turn answered within 10 seconds and cleanup checks found no leftover tmux sessions or containers on any host. The two claude cells marked with fixes failed first exactly as the pitfalls describe: local container stalled on the first-run theme dialog (start-scoped, credentials valid), and the linux box container failed its turn with `Login expired - Please run /login` while start, capture, stop, and cleanup all still worked (turn-scoped, despite prep reporting 14.5 days of refresh-token validity). Not covered by this matrix: network-partition, reattach-after-disconnect, and cross-host messaging scenarios (gated e2e tests, see `tests/e2e/TEST_PLAN.md`).
