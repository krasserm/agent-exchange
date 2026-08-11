# agent-dispatch

`agent-dispatch` turns an ordinary coding-agent session into a long-running dispatcher. You give that session a request; it resolves the relevant project, starts another `asn`-managed session there, sends the request, and hands control of the new session to you.

There is no dispatcher service or routing program in this directory. [`AGENTS.md`](AGENTS.md), also exposed to Claude Code as `CLAUDE.md`, defines the role. The actual work happens in the sessions the dispatcher starts.

The dispatcher is useful when you work across many repositories or regularly need scratch workspaces. It keeps project locations and launch rules out of individual prompts while leaving every delegated session directly accessible through its terminal UI, `asn`, [`agent-session-web`](../agent-session-web/), or [`agent-session-top`](../agent-session-top/).

## Requirements

- [`agent-session`](../agent-session/) installed with `asn` on `PATH`
- an authenticated supported agent
- the `agent-exchange` plugin when explicit team requests should create coordinators

## Start the dispatcher

From the repository root:

```bash
asn start "$PWD/agent-dispatch"
```

Attach to the printed `tmux` session or open it in one of the session interfaces. Then make requests normally:

```text
Fix the flaky integration test in the payments service.
Create a scratch prototype comparing these two APIs.
Form a team to review the migration across the frontend and backend.
```

For project work, the dispatcher reports the new session's directory and key and then stops acting on the request. You continue in the delegated session. Under normal operation, sessions remain running until you stop them; stopping or restarting the daemon also ends them.

## How requests are placed

| Request | Default action |
| --- | --- |
| Known project or routing rule | Start and brief a session there, then hand off. |
| Work without a known project | Create a fresh scratch directory and start a session there. |
| Quick factual or conversational question | Answer in the dispatcher session. |
| Explicit request for a team | Start and brief a local coordinator session, then hand off. |
| Project implied but location ambiguous | Ask which project is intended. |

A coordinator is another ordinary session with coordination instructions. The dispatcher does not supervise it unless the user explicitly keeps the request routed through the dispatcher.

## Configure project locations

Create `~/.agent-dispatch/locations.md` and describe the machine-specific names and rules the dispatcher should use. Keep paths absolute where possible.

```markdown
## Routing rules

- **Research**: `/Users/me/work/research/`; start with `--agent codex`.
- **Scratchpad**: create a fresh directory under `/Users/me/work/scratch/<slug>/`.

## Directory map

- `/Users/me/work/api/`: backend API
- `/Users/me/work/web/`: browser client
```

The file may specify launch options, preparation steps, or a first message in prose. The dispatcher reads it before classifying each request. If it is absent, only locations named explicitly by the user count as known projects; other work goes under `~/.agent-dispatch/scratch/`.
