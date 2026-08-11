# agent-session diagrams

Visual companion to [architecture.md](architecture.md), which remains the authoritative description. Two views: management (how sessions are controlled and observed) and messaging (how text reaches and leaves a session).

## Management: control plane

One local daemon owns the live registry. The full `asn` CLI talks to it over a Unix socket; the daemon drives the session's tmux terminal and learns everything about the agent by tailing the append-only records its hooks write.

```mermaid
flowchart LR
    callers["Humans, scripts, coordinators,<br/>local-native sessions"]
    subgraph ch["Control host"]
        cli["Full asn CLI"]
        daemon["Control daemon<br/>live registry (memory), per-target send locks,<br/>monitor, outbox watchers"]
        meta["meta.json<br/>key and transport metadata"]
    end
    subgraph rt["Session runtime"]
        tmux["tmux terminal"]
        agent["Agent CLI<br/>Claude Code or Codex"]
        records["events.jsonl, outbox.jsonl<br/>append-only session records"]
    end
    callers --> cli
    cli -- "Unix socket,<br/>one JSON object per line" --> daemon
    daemon -- "start, stop, send, capture" --> tmux
    tmux --> agent
    agent -- "hooks append" --> records
    records -- "monitor tails" --> daemon
    daemon -- "while managed" --> meta
```

What to keep in mind:

- The management key names everything; it is not the agent's conversation ID.
- The monitor projects hook events to a small status vocabulary (`working`, `waiting`, `idle`, `backgrounded`), so `idle` means no foreground work observed, not proof of completion.
- The daemon starts with `asn start` or `asn daemon start`, exits when the last session stops, and never restores its registry from disk.

## Management: placement composition

The diagram above is literal for a local-native session. Other placements insert transport layers between the daemon and the runtime. Remote container is the fullest case:

```mermaid
flowchart LR
    subgraph ch["Control host"]
        daemon["Control daemon"]
        outer["Outer tmux (proxy)<br/>terminal seen by humans and send"]
        mirror["Per-host mirror of<br/>remote ~/.agent-session"]
    end
    subgraph rh["Remote host (SSH)"]
        rstate["~/.agent-session<br/>events, outbox, launch state"]
        subgraph co["Owned container"]
            inner["Inner tmux<br/>owns the agent and scrollback"]
            agent["Agent CLI with hooks"]
        end
    end
    daemon -- "send, capture" --> outer
    outer -- "ssh + docker exec,<br/>reconnecting attach" --> inner
    inner --> agent
    agent -- "hooks append<br/>(bind mount)" --> rstate
    rstate -- "Mutagen one-way replica" --> mirror
    mirror -- "monitor tails<br/>mirrored files" --> daemon
```

The other placements are subsets of this picture:

- **Remote native** drops the container: the inner tmux and state directory live directly on the SSH host.
- **Local container** drops the SSH and mirror legs: the proxy attaches with `docker exec` alone, and the bind-mounted state is written directly on the control host.
- **Local native** drops everything: the agent runs in local tmux and writes local files.

The inner tmux survives link loss and is authoritative for capture. A vanished outer proxy means `disconnected`, not agent termination.

## Messaging: paths into and out of a session

All input converges on the same terminal composer. Reads by remote and container sessions go through a separate, read-only channel.

```mermaid
flowchart LR
    human["Human"]
    fullcli["Full asn CLI<br/>humans, scripts, local-native sessions"]
    peer["Restricted asn client<br/>remote and container sessions"]
    outbox["Caller's outbox.jsonl"]
    daemon["Control daemon"]
    read["TCP read endpoint<br/>read-only, bearer token"]
    target["Target session tmux"]
    agent["Target agent"]

    human -- "tmux attach" --> target
    fullcli -- "send over Unix socket" --> daemon
    peer -- "send appends<br/>{ts, from, to, message}" --> outbox
    outbox -- "local write, bind mount,<br/>or Mutagen mirror" --> daemon
    daemon -- "serialized per target:<br/>tmux paste and Enter" --> target
    target --> agent
    peer -- "messages, status, capture via<br/>reverse SSH tunnel or<br/>Docker host gateway" --> read
    read --> daemon
```

What to keep in mind:

- Outbox send is fire-and-forget: the sender gets no delivery result, and daemon-side delivery is at most once with no replay of records written before the watcher attached.
- The read endpoint cannot start, stop, or send. A valid token proves a live managed session but is not scoped to it as a read target.
- Direct human input bypasses daemon serialization; tmux and the agent composer are the only arbitration.

## Messaging: peer send, end to end

A remote session messaging another session, from append to composer submission:

```mermaid
sequenceDiagram
    participant A as Session A (remote)
    participant O as A's outbox.jsonl
    participant M as Control-host mirror
    participant D as Control daemon
    participant T as Session B tmux
    participant B as Agent B

    A->>O: asn send B "..." appends {ts, from, to, message}
    Note over A: returns immediately, no delivery result
    O-->>M: Mutagen one-way replication
    M-->>D: outbox watcher sees appended line
    D->>D: acquire B's send lock
    D->>T: paste via tmux buffer
    D->>T: press Enter
    alt acknowledgement within timeout
        B-->>D: UserPromptSubmit event
    else no acknowledgement
        D->>T: press Enter once more
        Note over D: absence of the event is not delivery failure:<br/>a busy agent may take the text as steering input
    end
    T->>B: composer submission
    B-->>D: turn events via events.jsonl (mirrored)
```

A successful send means the terminal submission attempt completed. Whether a turn started, finished, or produced a reply must be verified through `status`, `messages`, or resulting artifacts.
