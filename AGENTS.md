# Working on agent-exchange

Keep the README files in sync when public behavior changes.

## Repository map

| Area | Purpose | Start here |
| --- | --- | --- |
| [`agent-session/`](agent-session/) | Core `asn` CLI, daemon, session lifecycle, agent adapters, transports, observation, and messaging | [`agent-session/AGENTS.md`](agent-session/AGENTS.md), then `src/agent_session/` |
| [`agent-session-web/`](agent-session-web/) | FastAPI and Svelte browser UI over managed sessions and tmux terminals | `src/agent_session_web/app.py`, `cs_client.py`, and `terminal.py`; frontend work starts at `frontend/src/App.svelte` |
| [`agent-session-top/`](agent-session-top/) | Go terminal dashboard and direct terminal launcher | `main.go`, `model.go`, and `daemon.go` |
| [`agent-docker/`](agent-docker/) | Container image and standalone image helper | `Dockerfile` and `agent-docker.sh` |
| [`agent-dispatch/`](agent-dispatch/) | Instruction-only dispatcher role, with no dispatcher service | [`agent-dispatch/AGENTS.md`](agent-dispatch/AGENTS.md) |
| [`plugin/`](plugin/) | Installable Codex plugin manifest and session or coordination skills | `.codex-plugin/plugin.json` and `skills/` |
| [`.claude-plugin/`](.claude-plugin/) | Claude plugin marketplace metadata for the same plugin | `marketplace.json` |
| [`site/`](site/) | Static project site | `index.html` |
| [`docs/assets/`](docs/assets/) | Shared screenshots used by repository documentation | Image files in this directory |

Cross-component changes usually begin in `agent-session`. If the daemon JSON shape or status vocabulary changes, also inspect `agent-session-web/src/agent_session_web/models.py`, `agent-session-web/frontend/src/lib/types.ts`, `agent-session-web/frontend/src/lib/status.ts`, `agent-session-top/daemon.go`, and `agent-session-top/styles.go`.

## Tests

- `agent-session` test guidance lives in [`agent-session/AGENTS.md`](agent-session/AGENTS.md).
- Browser backend tests are in `agent-session-web/tests/unit/` and `agent-session-web/tests/integration/`. Frontend behavior currently has no separate test directory, so verify it with its build and the relevant backend integration tests.
- Terminal dashboard tests are the `*_test.go` files in `agent-session-top/`.
- `agent-docker`, `agent-dispatch`, the plugin metadata, and the static site have no dedicated automated suites. Verify the closest consuming component and perform a targeted manual check when changing them.

## Ground rules

- Do not modify `agent-session/AGENTS.md` or `agent-dispatch/AGENTS.md` unless a task explicitly asks for their local instructions to change.
- Do not recover deleted documentation from Git history or prior chats when a task asks for independent source inspection.
