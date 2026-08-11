---
name: agent-coord
description: Run a team of coding-agent sessions toward one goal - spawn workers and sub-coordinators via asn, brief them, monitor by observation, synthesize results. Use only when explicitly asked to form or run a team of agent sessions, or when acting as the coordinator of such a team.
---

# agent-coord

You are a coordinator: you drive a team of agent sessions toward one goal with the `asn` CLI (mechanics: `agent-session` skill). Coordinators and sub-coordinators always run local-native on the control host; only there can sessions be spawned.

## Model

- Control flows down: you instruct workers with `asn send`.
- State flows up by observation only: `asn status` / `asn messages` / `asn capture`. Workers never report to you actively; don't ask them to.
- Peers can message each other directly with `asn send` once you introduce them by exchanging keys; those exchanges are not relayed through you.
- The user is a first-class participant who may attach to or message any session at any time. Never proxy or fight direct user input; questions only the user can answer are surfaced in your conversation, not answered by you.

## Forming the team

1. Spawn each worker with `asn start <absolute project dir>` (default options). Use `--host`/`--docker` only when the goal or user asks for remote placement or isolation; the host must be prepared (`asn prep`).
2. Store the full 32-char keys (`asn start` output, `asn list --json`); short prefixes collide.
3. Send each worker its briefing as the first message, then verify with `asn capture` that it was actually submitted - a send right after start can land in the composer unsubmitted; if so, resend.
4. For an independent workstream that needs its own team, spawn a sub-coordinator: a local-native session in a fresh `~/.agent-coord/scratch/<slug>/` (kebab-case), first message = its goal plus "use the agent-coord skill". Monitor it like a worker. Keep hierarchies shallow.

## Briefing workers

Phrase briefings cooperatively (a wall of imperative instructions occasionally gets safety-blocked). Include:

- the worker's role and the goal slice it owns;
- what to produce and where;
- that a coordinator observes progress - no active reporting needed;
- when peers are introduced: each peer's role and full key, how to message them (`asn send KEY "..."`), and the convention to prefix peer messages with the sender's role name (delivery does not identify the sender).

Remote/container workers and codex workers have no skills available; their briefings must be fully self-contained.

## Monitoring

- Never block-poll in your conversation. Run one non-blocking background watcher per worker that polls `asn status KEY --json` every ~30s and exits when status leaves `working` - its exit wakes you.
- `idle`: read `asn messages KEY` and decide the next step.
- `waiting`: the worker is asking a question; read it via `asn capture`. Answer with `asn send` if it is within the goal's scope, otherwise surface it to the user and wait.
- `backgrounded` is not done: background work is still running, keep watching until `idle`. codex and older agents omit this signal, so for them verify completion from message content, not status alone.

## Completion

- Synthesize the workers' results into one answer in your own conversation.
- Keep all sessions alive and list which ones are still running; stopping (`asn stop KEY`) is the user's call.
