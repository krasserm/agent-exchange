# agent-session architecture

`agent-session` wraps an interactive agent terminal with a common lifecycle, observation, and messaging layer. The agent still owns its conversation and TUI. The control daemon owns the set of sessions that can currently be addressed by `asn`. [diagrams.md](diagrams.md) illustrates the management and messaging views described here.

## Identity and state ownership

Each start creates a random 32-character management key. That key names daemon state, tmux resources, event and outbox files, and public operations. It is not the agent's conversation ID. Resuming a conversation creates a fresh management key.

| State | Owner | Lifetime |
| --- | --- | --- |
| Live registry and per-target send locks | Control daemon | Memory only |
| Management key and transport metadata | Control daemon | `meta.json` while managed |
| Conversation and transcript | Agent CLI | Agent-defined |
| Terminal process and scrollback | tmux where the agent runs | Until agent, tmux, or managed runtime ends |
| Lifecycle and outbox records | Agent host state directory | Append-only records retained after stop |
| Project and credentials | Runtime host | Outside daemon ownership |

The daemon registers a session only after its TUI is ready. Claude Code readiness comes from its launch event. Codex readiness comes from a live composer marker and may precede assignment of a conversation ID. A ready session with a null conversation ID is therefore valid.

## Control protocol

The full CLI talks to one local daemon over a Unix socket. A request and response are each one JSON object followed by a newline. Requests carry a method and parameters; responses carry either a result or an error. The daemon addresses sessions through its live registry and resolves full keys or unambiguous prefixes. Terminal sends are serialized per target; lifecycle requests are not globally serialized.

Only start and explicit daemon startup may create a daemon. Polling operations such as list do not resurrect one. The Unix endpoint exposes lifecycle mutations, terminal input, reads, and cleanup. It is an administrative interface whose security boundary is access to the local account and socket.

Start generates the key and read token, prepares the selected placement, creates tmux ownership, launches the agent with hooks, waits for readiness, then records metadata and starts the outbox watcher. Stop removes the session from the registry, stops observation, kills owned tmux resources, removes an owned container when present, and removes `meta.json`. Inner teardown over an unreachable SSH link is best effort: control state can be removed while a remote process survives.

## Observation protocol

Agent hooks append normalized JSON lines to a per-session `events.jsonl`. Events carry the agent conversation ID, projected status, optional transcript path, optional submitted prompt, optional last assistant response, and any background-task counts the agent reports.

The daemon tails only records written after its monitor starts. It reopens the file for each update so atomic replacement by a mirror does not break the byte offset. Malformed or incomplete lines are skipped. There is no event replay into a newly created monitor.

The status projection is intentionally smaller than either agent's native lifecycle:

- prompt and tool activity project to `working`;
- permission or elicitation events project to `waiting`;
- a successful turn end projects to `idle`, or `backgrounded` when Claude reports pending background work;
- a session-end event removes live agent information;
- subagent lifecycle events do not overwrite the parent session's status.

Codex and older agent versions may not report background counts, so `idle` means no foreground work is currently observed, not proof that all work is complete. Codex also does not emit every lifecycle event Claude emits, including a dependable session-end event.

Recent assistant responses are an in-memory projection of turn-end events, capped at 50 entries. Terminal capture is independent of this projection and can show errors or output for a turn that produced no observed assistant response.

## Terminal submission

Human input, full-CLI input, and peer-delivered input converge on the same agent TUI. A daemon send is serialized per target, pasted through a tmux buffer so multiline text remains one composer submission, then followed by Enter.

For a new prompt, the daemon listens for `UserPromptSubmit` before pressing Enter, which avoids missing a fast acknowledgement. It retries Enter once if no acknowledgement arrives. Absence of that event is not treated as delivery failure because a busy agent can accept the text as steering for its current turn without starting a new prompt event. Consequently, a successful send means the terminal submission attempt completed, not that a turn started or finished.

Slash commands are TUI commands rather than prompts. They receive one Enter and are not confirmation-waited. Direct terminal input bypasses daemon send serialization but is still observed when the agent emits hooks. Concurrent human and programmatic input is not arbitrated beyond tmux and the agent composer.

## Transport composition

Location and runtime are independent choices:

| Placement | Agent tmux | Local terminal | Session records | In-session reads |
| --- | --- | --- | --- | --- |
| Local native | Local tmux | Same tmux | Direct local writes | Full local CLI |
| Local container | Inner tmux in one owned container | Local proxy attached with `docker exec` | Host bind mount | Docker host gateway to daemon TCP |
| Remote native | Inner tmux on SSH host | Local SSH proxy | Remote writes, one-way mirrored | Per-session SSH reverse tunnel on remote loopback |
| Remote container | Inner tmux in one container on SSH host | Local SSH plus `docker exec` proxy | Container bind mount, then one-way mirrored | Container host gateway to a per-session SSH reverse tunnel |

Every non-local-native placement uses an inner tmux that owns the agent and an outer local tmux that owns a reconnecting attachment. The inner tmux survives client detachment and link loss. It is also the authoritative source for capture and scrollback. The outer tmux is the terminal presented to humans and to send operations.

For remote placements, the long-lived SSH attachment also owns the reverse read tunnel. Its reconnect loop retries while the inner tmux can still exist and exits only after it can positively determine that the inner session is gone. Killing the outer tmux reports `disconnected` without implying agent termination. Reattachment recreates the outer proxy and confirms that an inner tmux client attached before reporting success. Public terminal operations remain unavailable while the proxy is absent.

Remote-container tunnels request a non-loopback remote bind so `host.docker.internal` can reach them. Linux hosts require an sshd `GatewayPorts` policy that permits this. Docker Desktop on macOS can reach a loopback-downgraded bind through its host-gateway routing.

## Mounts and replication

An owned container receives read-write mounts for the selected project, the agent's container configuration, and the agent-session host state. It may receive a read-only Git configuration. The project appears at a fixed workspace path and the state mount carries hooks, the restricted client, launch state, events, and outbox records. Containers run as a non-root development user with dropped capabilities, no-new-privileges, memory and process limits, and ordinary network access.

Each remote host has one Mutagen `one-way-replica` from the host's `~/.agent-session` to a control-host mirror. Native and container sessions on that host share it. Polling is forced on the remote side so bind-mounted container writes are detected reliably. The daemon watches the mirror with the same logic used for local files.

The replica includes session records and deployed plugin state. It does not mirror the project, agent credentials, agent configuration, transcripts outside the session tree, tmux state, the local proxy, daemon metadata, or the in-memory registry. Writes or deletions in the local mirror never flow back to the agent host.

## Peer messaging

Local-native sessions can reach the full CLI and therefore the Unix control protocol. Other placements receive a Bash and `jq` client with two deliberately different paths:

1. `send` appends one `{ts, from, to, message}` record to the caller's outbox. A daemon watcher sees the local write, bind-mounted write, or mirrored write and submits the message to the target terminal.
2. `messages`, `status`, and `capture` use a line-JSON TCP endpoint on the control host. The session presents its random bearer token. Remote sessions reach this endpoint through the reverse tunnel; local containers use the Docker host gateway. The endpoint also permits `scrollback`, although the bundled restricted client does not expose that command.

The TCP endpoint is read-only. Even a valid token cannot start, stop, or send. A valid token authenticates a live managed session but is not scoped to that session as a read target, so it can inspect any known key through the allowed methods.

Outbox delivery is at most once from the daemon watcher's point of view, with no durable acknowledgement, retry queue, or deduplication identifier. Watchers begin at the current end of an existing outbox, so records written before watcher attachment are not replayed. An unknown target or terminal failure is logged by the daemon, but the append-only sender has already returned success and receives no delivery result. The `from` field is informational; authorization comes from the runtime's ability to append to the watched outbox path.

## Persistence and failure semantics

The live registry is never restored. Graceful daemon shutdown stops all registered sessions. After an ungraceful daemon exit, tmux or container resources can remain alive, but read-only CLI calls see no sessions. A new daemon snapshots surviving metadata before accepting new starts, serves requests, and reaps those orphans in background. It does not adopt them.

Stopping a session retains its directory but removes its metadata. On the control host, ended directories with no metadata are pruned after seven days during daemon startup maintenance. Remote host session directories and reusable per-host Mutagen syncs are not removed by ordinary stop, so remote records can outlive the control-host retention pass.

The monitor and assistant-message buffer do not replay retained events. Retained files are diagnostic evidence, not a recoverable message queue or session database. Agent transcripts follow the agent's own persistence rules, and a transcript path reported from a remote or container runtime may name a path that does not exist on the control host.

A reported session end makes terminal mutations inactive but leaves the management key registered until stop. When an agent omits a session-end event, local-native sessions can infer termination from a vanished tmux. For proxied sessions, a vanished outer tmux means `disconnected`, so dependable agent-exit detection requires either an agent lifecycle event or explicit cleanup.

A dropped SSH link leaves the inner tmux and append-only remote records intact. The proxy and Mutagen independently reconnect. Status may remain at the last observed value while the proxy process is still retrying; `disconnected` specifically denotes an absent outer tmux. Inputs have no end-to-end delivery acknowledgement, so callers must verify lifecycle events, assistant output, or resulting artifacts.

## Trust boundaries

- The Unix socket is the administrative boundary. A process that can use the full local CLI can create, message, inspect, reattach, and stop sessions.
- Remote and container runtimes receive only outbox send and token-gated reads, not lifecycle mutation. Code with access to their mounted state can still read the token or append messages to any known target key.
- The TCP endpoint binds beyond loopback for container reachability, uses bearer tokens rather than transport encryption, and trusts the local, Docker-gateway, or SSH-tunnel path to protect those tokens.
- SSH hosts are trusted execution hosts. Their account administrators can inspect the agent process, project, session records, and injected token.
- Managed Docker reduces accidental host impact but is not a hostile-code sandbox. The runtime has network access and read-write project and credential mounts, and it shares the host kernel.
- Anyone attached to the tmux terminal has direct control of the agent conversation and can race programmatic input.
