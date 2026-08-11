# agent-session-web

`agent-session-web` is a browser interface for all sessions managed by the local `agent-session` daemon. It combines the session list, status, recent assistant responses, message and stop controls, and a live terminal for the selected session.

`agent-session` launches each agent in tmux, and the web application makes those existing terminals available in the browser. You can switch among local, remote, and container sessions and work directly with each agent's interactive UI.

<a href="../docs/assets/agent-session-web.png" target="_blank" rel="noopener noreferrer"><img src="../docs/assets/agent-session-web.png" alt="agent-session-web showing local and remote coding-agent sessions"></a>

## Requirements

- [`agent-session`](../agent-session/) installed with `asn` on `PATH`
- Python 3.13 or newer and [`uv`](https://docs.astral.sh/uv/)
- Node.js 18 or newer and `npm`
- `tmux` on `PATH`

The interface can start with no running sessions and updates when one appears.

## Start locally

From the repository root:

```bash
cd agent-session-web
AGENT_SESSION_WEB_HOST=127.0.0.1 ./run.sh
```

Open [http://127.0.0.1:8770](http://127.0.0.1:8770). The script installs missing frontend dependencies, builds the Svelte application, and starts the Python server. Use `./run.sh --build` to force a frontend rebuild.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENT_SESSION_WEB_HOST` | `127.0.0.1` in Python; `0.0.0.0` in `run.sh` | Bind address. |
| `AGENT_SESSION_WEB_PORT` | `8770` | Listen port. |
| `AGENT_SESSION_WEB_CS_BINARY` | `asn` | Path or command used for session operations. |
| `AGENT_SESSION_WEB_CS_TIMEOUT` | `10` | Timeout in seconds for `asn` calls. |
| `AGENT_SESSION_WEB_DIST_DIR` | `frontend/dist` | Built frontend directory. |

## Development

```bash
uv sync
npm --prefix frontend install
npm --prefix frontend run build
uv run pytest
uv run agent-session-web
```

For frontend hot reload, run `uv run uvicorn agent_session_web.app:app --reload --port 8770` and `npm --prefix frontend run dev` in separate terminals. Vite listens on port 5173 and proxies API and WebSocket traffic to the backend.
