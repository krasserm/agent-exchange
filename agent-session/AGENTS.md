# Working on agent-session

`agent-session` is the core of `agent-exchange`. Read the [README](README.md) for the public model and [architecture](docs/architecture.md) for the session-management and messaging protocols. Use this file for codebase navigation.

## Commands

```bash
uv sync
uv run pytest
uv run pytest tests/e2e -v --agents claude,codex
uv run python tests/repl.py "$PWD" --agent codex
uv run asn --help
```

The end-to-end suite starts authenticated agents and may consume provider quota. It uses an isolated state root configured by `tests/conftest.py`; see [`tests/e2e/TEST_PLAN.md`](tests/e2e/TEST_PLAN.md) for target selection. Run placement-specific `asn prep ... --check` before interpreting a transport skip or readiness failure. To verify the full transport-placement matrix with both agents, follow [`docs/verification.md`](docs/verification.md).

## Source layout

Modules under [`src/agent_session/`](src/agent_session/) are named for their responsibility (`cli`, `_daemon`, `_session`, `_tmux`, `_monitor`, `_models`, `_transport`, `_provision`, `_prep`, `_mirror`, `_naming`, `agents/`), each carries a docstring, and test files in `tests/` follow the source module names. Start from the module matching the change and its same-named test file. Locations that do not follow from the naming:

- Lifecycle hooks that agents run live in `src/agent_session/plugin/log-event.sh`.
- The restricted in-session `asn` client deployed to remote and container runtimes is `src/agent_session/plugin/asn`; `_provision.py` deploys it.
- Daemon JSON shape or status vocabulary changes ripple into `agent-session-web` and `agent-session-top`; the root [`AGENTS.md`](../AGENTS.md) lists the exact files.

## Invariants to preserve

- Claude Code and Codex share the daemon, transport, tmux, and public CLI layers. Agent-specific behavior stays behind the `Agent` abstraction.
- Every managed runtime gets a full 32-character key. Do not confuse it with the agent's conversation ID or the read bearer token, and do not persist only the display prefix.
- Agent hooks append the shared event schema. Some agents emit less lifecycle data, so session IDs and transcript paths may be absent and readiness may come from terminal markers.
- Sends to one target are serialized. Busy agents may accept steering input without a prompt-submitted event; absence of that event is not proof of failed delivery and must not trigger replay.
- Local-native runs directly in local tmux. Other placements run an inner tmux and a reconnecting local proxy; remote state reaches the daemon through a per-host Mutagen mirror.
- The daemon starts with `asn start` or `asn daemon start` and exits when the last managed session stops. Startup reaps abandoned resources; it does not restore them to the live registry.
- `asn daemon stop` ends every managed session. Tests and cleanup code must not use it as a substitute for stopping one key.

## Adding an agent

Implement the `Agent` abstraction under `src/agent_session/agents/`, register the adapter, and map its lifecycle hooks to the shared event schema. Keep agent-specific launch flags, readiness signals, paths, credentials, and capabilities in the adapter. Add unit coverage in `tests/test_agents.py`; the end-to-end suite will pick up the registered agent when selected.

When a public behavior or boundary changes, update the nearest README and keep architecture files as maps rather than implementation narration.
