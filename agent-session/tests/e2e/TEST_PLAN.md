# End-to-end tests

These tests start authenticated Claude Code or Codex processes in real tmux sessions. Transport tests may also use Docker, SSH, and Mutagen. They consume provider quota and are not part of the default test run.

## Run by agent

```bash
uv run pytest tests/e2e -v --agents claude
uv run pytest tests/e2e -v --agents codex
uv run pytest tests/e2e -v --agents claude,codex
```

Without `--agents`, both supported agents are selected. Use ordinary pytest paths and `-k` expressions to narrow the scenarios.

## Run by transport

Transport tokens have the form `LOCATION-RUNTIME` with an optional remote host:

```text
local-native
local-container
remote-native@example-host
remote-container@example-host
```

For example:

```bash
uv run pytest tests/e2e/test_transport_e2e.py -v \
  --agents codex \
  --targets local-native,local-container,remote-native@example-host
```

Unavailable targets are skipped after prerequisite checks. Prepare a target with the same agent and placement before treating a skip as a product failure:

```bash
asn prep --host example-host --docker --agent codex --check
asn prep --host example-host --docker --agent codex
```

## Requirements

| Target | Required environment |
| --- | --- |
| Local native | Authenticated selected agent and tmux. |
| Local container | Docker, prepared `agent-docker` image, and container credentials. |
| Remote native | Prompt-free SSH, selected agent, tmux and `jq` remotely, and Mutagen locally. |
| Remote container | Prompt-free SSH, Docker and container credentials remotely, and Mutagen locally. |

The suite isolates `ASN_HOME`, `ASN_SOCKET_PATH`, and `ASN_READ_PORT` through `tests/conftest.py`; it does not use the normal daemon state.

## Human prerequisites: confirm with the user before a run

Some failure causes can only be fixed by a person at an interactive prompt, and `asn prep --check` does not reliably catch them. Before a run that includes the affected placements, prompt the user to confirm these, and point them at the exact commands below rather than the failing test:

- **Claude container credentials expire independently of prep's report.** Container cells read `~/.claude-docker` on the runtime host. A turn can fail with `Login expired - Please run /login` even when prep reports weeks of refresh-token validity (observed 2026-08-11: prep said 14.5 days, the first turn failed). Symptom: start, capture, and stop pass, `test_send_and_receive` fails with "no assistant message". Fix: `agent-docker.sh login` on that host.
- **A fresh claude container config stalls startup on the first-run theme dialog.** `agent-docker.sh` seeds `~/.claude-docker/.claude.json` with `{}` when missing; without completed onboarding, claude opens its theme chooser, which the startup-prompt dismisser does not handle. Symptom: every test on the placement errors with `No SessionStart event received` while credentials are valid (`claude -p` in the container works). Fix: run claude interactively in the container once and choose a theme.
- **Remote-native claude reads file credentials, not the macOS keychain.** A working GUI claude on the host proves nothing. Fix: `claude login` from an SSH session on that host. See the pitfalls in [`../../docs/verification.md`](../../docs/verification.md).
- **Codex credentials cannot be validated offline.** Prep reports them present without checking validity; only a real turn proves them.

## Optional destructive/network scenarios

Some tests are gated because they interrupt connectivity or require more than one remote host:

| Variable | Enables |
| --- | --- |
| `ASN_E2E_NETWORK=1` | Network-partition and reconnection scenarios. |
| `ASN_E2E_CROSS_HOST=1` | Cross-host message routing. |
| `ASN_E2E_HOST_A`, `ASN_E2E_HOST_B` | Hosts used by the cross-host test. |

Read [`conftest.py`](conftest.py), [`helpers.py`](helpers.py), and the selected test module for current fixture behavior and assertions.
