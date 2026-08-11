# agent-session-top

`agent-session-top` is a terminal dashboard for the sessions managed by the local `agent-session` daemon. It shows the current session list and latest assistant response, can send a message or stop a session, and can open the selected agent's `tmux` terminal in a separate macOS terminal window.

The dashboard reads session state from the same daemon used by `asn` and sends message and stop requests back to it. Local, remote, and container sessions appear together because placement is handled by `agent-session`.

## Requirements

- Go 1.25.7 or a compatible newer release
- [`agent-session`](../agent-session/)
- macOS with iTerm2 or Terminal.app for the open-terminal action

The dashboard itself can display and control sessions on other platforms, but opening a new terminal window is currently implemented with macOS automation.

## Build and run

From the repository root:

```bash
cd agent-session-top
go build -o asn-top .
./asn-top
```

The dashboard can start while the daemon is absent and will keep polling. Start a session when you want data to appear:

```bash
asn start "$HOME/Development/my-project"
./asn-top
```

## Keys

| Key | Action |
| --- | --- |
| `Tab` | Move focus between the session list and response pane. |
| `j`, `k`, arrows | Select a session or scroll the focused pane. |
| `PageDown`, `Ctrl-d` | Scroll responses down. |
| `PageUp`, `Ctrl-u` | Scroll responses up. |
| `o` | Open the selected session in iTerm2 or Terminal.app. |
| `s` | Send a message to the selected session. |
| `x` | Stop the selected session after confirmation. |
| `r` | Retry the daemon connection. |
| `q`, `Ctrl-c` | Quit the dashboard. |

Stopping a session ends the managed agent runtime. Quitting the dashboard does not.

## Development

```bash
go test ./...
```
